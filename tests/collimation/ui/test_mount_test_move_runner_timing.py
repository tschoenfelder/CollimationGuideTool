"""MountTestMoveRunner timing (issue #53, S3b): every runner wait -- the fresh
park reading (S6.0c), the unpark/re-park confirmation, the stop_tracking gate
delay, rejection retries and the settle -- and what Stop does in each of them.

Two sections:

- `TestCharacterizationBeforeS3b` was written and run green BEFORE the clock
  injection (on the real clock, with the module's timing constants patched
  down to milliseconds). It runs unchanged after S3b and so proves the
  migration did not change the observable behaviour.
- The fake-time sections drive the same waits through an injected `FakeClock`
  (auto-advance, or manual mode with `wait_for_sleepers` where a test must
  observe the worker while it waits): exact durations, deadline boundaries
  before/at/after, and Stop in each wait state, with no real waiting.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest
from astrotool_core.mount.park_port import MountParkPort, MountParkStatus
from astrotool_core.mount.port import AxisDirection, CommandResult, MountAxis
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.timing import SYSTEM_CLOCK, FakeClock
from collimation_tool.ui import mount_test_move_runner as runner_module
from collimation_tool.ui.mount_test_move_runner import MountPulseOutcome, MountTestMoveRunner

_A1P = (MountAxis.AXIS1, AxisDirection.POSITIVE)


def _drain(runner: MountTestMoveRunner, *, timeout_s: float = 5.0) -> MountPulseOutcome:
    """Test-side synchronization only (real time, bounded): waits for the worker
    thread to publish its outcome."""
    deadline = time.monotonic() + timeout_s
    while runner.is_busy:
        assert time.monotonic() < deadline, "runner never finished"
        time.sleep(0.002)
    outcome = runner.take_latest()
    assert outcome is not None
    return outcome


class _ScriptedPark(MountParkPort):
    """A park port whose `status()` is scripted per call: `script(call_index)` returns
    (parked, fresh). Counts every command so a test can prove nothing was sent."""

    def __init__(self, script: Callable[[int], tuple[bool, bool]]) -> None:
        self._script = script
        self.status_calls = 0
        self.commands: list[str] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return True

    def status(self) -> MountParkStatus:
        parked, fresh = self._script(self.status_calls)
        self.status_calls += 1
        return MountParkStatus(available=True, parked=parked, tracking=False, fresh=fresh)

    def park(self) -> None:
        self.commands.append("park")

    def unpark(self) -> None:
        self.commands.append("unpark")

    def stop_tracking(self) -> None:
        self.commands.append("stop_tracking")

    def start_tracking(self) -> None:
        self.commands.append("start_tracking")


class _StopOnStatusCall(_ScriptedPark):
    """Presses Stop (runner.abort()) from inside the `n`-th status() call -- i.e. while the
    worker is inside a park wait -- deterministically, without any timing."""

    def __init__(
        self, script: Callable[[int], tuple[bool, bool]], runner: MountTestMoveRunner, n: int
    ) -> None:
        super().__init__(script)
        self._runner = runner
        self._n = n

    def status(self) -> MountParkStatus:
        if self.status_calls == self._n:
            self._runner.abort()
        return super().status()


class _StopDuringMove(FakeMountAdapter):
    """Presses Stop while the (accepted) pulse is in the adapter."""

    def __init__(self, runner: MountTestMoveRunner) -> None:
        super().__init__()
        self._runner = runner

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        result = super().pulse_axis(axis, direction, duration_ms, rate_preset=rate_preset)
        self._runner.abort()
        return result


class TestCharacterizationBeforeS3b:
    """Written BEFORE the clock injection and green on the old code (real clock, constants
    patched to milliseconds); unchanged afterwards -- the neutrality proof."""

    @pytest.fixture(autouse=True)
    def _fast_constants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(runner_module, "_UNPARK_TIMEOUT_S", 0.05)
        monkeypatch.setattr(runner_module, "_REPARK_TIMEOUT_S", 0.05)
        monkeypatch.setattr(runner_module, "_PARK_POLL_INTERVAL_S", 0.005)
        monkeypatch.setattr(runner_module, "_PULSE_REJECTION_RETRY_DELAY_S", 0.001)

    def test_a_park_state_never_read_freshly_sends_nothing_at_all(self) -> None:
        park = _ScriptedPark(lambda _i: (True, False))  # connection busy throughout
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()
        runner.submit(park, mount, *_A1P, 100, park_after=False)
        outcome = _drain(runner)
        assert outcome.pulsed is False
        assert outcome.error == runner_module._PARK_STATE_UNKNOWN
        assert park.commands == []  # not even unpark/stop_tracking
        assert mount.pulse_log == []
        assert outcome.motion_started_at is None and outcome.settled_at is None

    def test_an_unpark_confirmation_lost_to_a_busy_connection_says_so(self) -> None:
        # Decision reading fresh+parked; afterwards every reading is held over (busy).
        park = _ScriptedPark(lambda i: (True, i == 0))
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()
        runner.submit(park, mount, *_A1P, 100, park_after=False)
        outcome = _drain(runner)
        assert outcome.pulsed is False
        assert outcome.error is not None
        assert "stayed busy (no fresh reading)" in outcome.error
        assert park.commands == ["unpark"]
        assert mount.pulse_log == []

    def test_an_unpark_never_confirmed_on_fresh_readings_says_not_confirmed(self) -> None:
        park = _ScriptedPark(lambda _i: (True, True))  # stays parked, readings fresh
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()
        runner.submit(park, mount, *_A1P, 100, park_after=False)
        outcome = _drain(runner)
        assert outcome.error is not None and "(not confirmed)" in outcome.error
        assert mount.pulse_log == []

    def test_a_rejected_pulse_is_attempted_exactly_the_retry_budget_then_reported(
        self,
    ) -> None:
        mount = FakeMountAdapter(reject_first_n_pulses=999)
        mount.connect()
        runner = MountTestMoveRunner()
        runner.submit(FakeMountPark(start_parked=True), mount, *_A1P, 100, park_after=False)
        outcome = _drain(runner)
        assert outcome.pulsed is False
        assert mount._pulse_attempt_count == runner_module._PULSE_REJECTION_RETRIES  # noqa: SLF001
        assert outcome.error is not None and "still parked?" in outcome.error

    def test_stop_pressed_during_the_fresh_park_wait_sends_no_step(self) -> None:
        runner = MountTestMoveRunner()
        # 1st reading held over (busy) -> the worker waits; Stop arrives on the 2nd poll,
        # which is fresh and parked.
        park = _StopOnStatusCall(lambda i: (i < 2, i >= 1), runner, n=1)
        mount = FakeMountAdapter()
        mount.connect()
        runner.submit(park, mount, *_A1P, 100, park_after=False)
        outcome = _drain(runner)
        assert outcome.pulsed is False
        assert outcome.error is not None and "stopped by the user" in outcome.error
        assert mount.pulse_log == []

    def test_stop_during_the_last_move_skips_the_settle(self) -> None:
        runner = MountTestMoveRunner()
        mount = _StopDuringMove(runner)
        mount.connect()
        started = time.monotonic()
        runner.submit(
            FakeMountPark(start_parked=True),
            mount,
            *_A1P,
            100,
            park_after=False,
            settle_ms=5000,
        )
        outcome = _drain(runner, timeout_s=3.0)
        assert time.monotonic() - started < 3.0  # the 5 s settle never ran
        assert outcome.pulsed is True  # the move itself was accepted
        assert outcome.error is None
        assert outcome.settled_at is not None and outcome.motion_ended_at is not None
        assert outcome.settled_at - outcome.motion_ended_at < 1.0


# --------------------------------------------------------------------------- fake time


class _TimedPark(MountParkPort):
    """A park port living on a `FakeClock`: readings are held over (fresh=False) until
    `fresh_from` (and again from `busy_from` on); an unpark/park command takes
    `unpark_delay_s`/`park_delay_s` of clock time to show up in the readings (None =
    never). Every command and reading is stamped with clock time."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        parked: bool = True,
        fresh_from: float = 0.0,
        busy_from: float | None = None,
        unpark_delay_s: float | None = 0.0,
        park_delay_s: float | None = 0.0,
    ) -> None:
        self._clock = clock
        self._parked = parked
        self._fresh_from = fresh_from
        self._busy_from = busy_from
        self._unpark_delay_s = unpark_delay_s
        self._park_delay_s = park_delay_s
        self._transition: tuple[bool, float] | None = None  # (target parked, at)
        self.commands: list[tuple[str, float]] = []
        self.readings: list[float] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return True

    def status(self) -> MountParkStatus:
        now = self._clock.monotonic()
        self.readings.append(now)
        if self._transition is not None and now >= self._transition[1]:
            self._parked = self._transition[0]
            self._transition = None
        fresh = now >= self._fresh_from and (self._busy_from is None or now < self._busy_from)
        return MountParkStatus(available=True, parked=self._parked, tracking=False, fresh=fresh)

    def _command(self, name: str, target: bool, delay: float | None) -> None:
        now = self._clock.monotonic()
        self.commands.append((name, now))
        if delay is not None:
            self._transition = (target, now + delay)

    def park(self) -> None:
        self._command("park", True, self._park_delay_s)

    def unpark(self) -> None:
        self._command("unpark", False, self._unpark_delay_s)

    def stop_tracking(self) -> None:
        self.commands.append(("stop_tracking", self._clock.monotonic()))

    def start_tracking(self) -> None:
        self.commands.append(("start_tracking", self._clock.monotonic()))


class _StampedMount(FakeMountAdapter):
    """Connected `FakeMountAdapter` that records the clock time of every pulse attempt."""

    def __init__(self, clock: FakeClock, *, reject_first_n_pulses: int = 0) -> None:
        super().__init__(reject_first_n_pulses=reject_first_n_pulses)
        self._clock = clock
        self.attempted_at: list[float] = []
        self.connect()

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        self.attempted_at.append(self._clock.monotonic())
        return super().pulse_axis(axis, direction, duration_ms, rate_preset=rate_preset)


@pytest.fixture
def exact_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binary-exact poll interval and timeouts, so the boundary instants are exact."""
    monkeypatch.setattr(runner_module, "_PARK_POLL_INTERVAL_S", 0.25)
    monkeypatch.setattr(runner_module, "_UNPARK_TIMEOUT_S", 1.0)
    monkeypatch.setattr(runner_module, "_REPARK_TIMEOUT_S", 1.0)


def _run(
    clock: FakeClock,
    park: MountParkPort,
    mount: FakeMountAdapter,
    *,
    settle_ms: int = 0,
    park_after: bool = False,
    runner: MountTestMoveRunner | None = None,
) -> MountPulseOutcome:
    runner = runner or MountTestMoveRunner(clock=clock)
    assert runner.submit(park, mount, *_A1P, 100, park_after=park_after, settle_ms=settle_ms)
    return _drain(runner)


class TestInjectedClock:
    def test_the_runner_defaults_to_the_real_clock(self) -> None:
        assert MountTestMoveRunner()._clock is SYSTEM_CLOCK  # noqa: SLF001

    def test_outcome_stamps_come_from_the_injected_clock(self) -> None:
        clock = FakeClock(start=1000.0)
        outcome = _run(clock, _TimedPark(clock), _StampedMount(clock), settle_ms=500)
        assert outcome.motion_started_at == 1000.0
        assert outcome.motion_ended_at == 1000.0
        assert outcome.settled_at == 1000.5


class TestUnparkConfirmationDeadline:
    """`_wait_for_parked(want_parked=False)`: read -> deadline check -> wait 0.25 s, deadline
    1.0 s after the wait starts. A reading taken exactly AT the deadline still counts."""

    @pytest.mark.parametrize(
        ("confirmed_after_s", "sleeps"),
        [(0.75, [0.25] * 3), (1.0, [0.25] * 4)],
        ids=["before_deadline", "exactly_at_deadline"],
    )
    def test_a_confirmation_up_to_the_deadline_is_accepted(
        self, exact_polling: None, confirmed_after_s: float, sleeps: list[float]
    ) -> None:
        clock = FakeClock()
        mount = _StampedMount(clock)
        outcome = _run(clock, _TimedPark(clock, unpark_delay_s=confirmed_after_s), mount)
        assert outcome.pulsed is True and outcome.error is None
        assert clock.sleeps == sleeps
        assert mount.attempted_at == [confirmed_after_s]

    def test_a_confirmation_after_the_deadline_aborts_without_moving(
        self, exact_polling: None
    ) -> None:
        clock = FakeClock()
        mount = _StampedMount(clock)
        outcome = _run(clock, _TimedPark(clock, unpark_delay_s=1.25), mount)
        assert outcome.pulsed is False
        assert outcome.error is not None and "unparked in time (not confirmed)" in outcome.error
        assert clock.sleeps == [0.25] * 4  # no wait after the final (deadline) reading
        assert clock.monotonic() == 1.0
        assert mount.attempted_at == []

    def test_a_connection_busy_through_the_deadline_is_reported_as_such(
        self, exact_polling: None
    ) -> None:
        clock = FakeClock()
        # fresh for the unpark decision at t=0, held over from t=0.25 on
        park = _TimedPark(clock, busy_from=0.25, unpark_delay_s=0.5)
        outcome = _run(clock, park, _StampedMount(clock))
        assert outcome.error is not None
        assert "stayed busy (no fresh reading)" in outcome.error
        assert clock.monotonic() == 1.0


class TestFreshParkReadingDeadline:
    """S6.0c's fresh-only unpark decision waits at most `_UNPARK_TIMEOUT_S` for a fresh
    reading; nothing at all is sent when none arrives."""

    @pytest.mark.parametrize("fresh_from", [0.5, 1.0], ids=["before_deadline", "at_deadline"])
    def test_a_fresh_reading_up_to_the_deadline_is_used(
        self, exact_polling: None, fresh_from: float
    ) -> None:
        clock = FakeClock()
        park = _TimedPark(clock, fresh_from=fresh_from)
        outcome = _run(clock, park, _StampedMount(clock))
        assert outcome.pulsed is True
        assert park.commands == [("unpark", fresh_from)]

    def test_no_fresh_reading_by_the_deadline_sends_nothing(self, exact_polling: None) -> None:
        clock = FakeClock()
        park = _TimedPark(clock, fresh_from=1.25)
        mount = _StampedMount(clock)
        outcome = _run(clock, park, mount)
        assert outcome.error == runner_module._PARK_STATE_UNKNOWN
        assert park.commands == [] and mount.attempted_at == []
        assert park.readings == [0.0, 0.25, 0.5, 0.75, 1.0]


class TestGateDelayAndRetries:
    def test_an_unparked_mount_waits_the_gate_delay_before_the_first_attempt(self) -> None:
        clock = FakeClock()
        park = _TimedPark(clock, parked=False)
        mount = _StampedMount(clock)
        outcome = _run(clock, park, mount)
        assert outcome.pulsed is True
        assert park.commands == [("stop_tracking", 0.0)]
        assert clock.sleeps == [runner_module._PULSE_REJECTION_RETRY_DELAY_S]
        assert mount.attempted_at == [0.3]

    @pytest.mark.parametrize(
        ("rejections", "pulsed"),
        [(5, True), (6, False)],
        ids=["accepted_on_the_last_allowed_attempt", "budget_exhausted"],
    )
    def test_the_retry_budget_boundary(self, rejections: int, pulsed: bool) -> None:
        clock = FakeClock()
        mount = _StampedMount(clock, reject_first_n_pulses=rejections)
        outcome = _run(clock, _TimedPark(clock), mount, settle_ms=2000)
        assert outcome.pulsed is pulsed
        # 6 attempts at most, 0.3 s apart; no wait after the last attempt
        assert mount.attempted_at == pytest.approx([0.3 * i for i in range(6)])
        retry_waits = [0.3] * 5
        assert clock.sleeps == (retry_waits + [2.0] if pulsed else retry_waits)


class TestSettle:
    def test_the_settle_is_exactly_settle_ms_after_the_last_move(self) -> None:
        clock = FakeClock()
        outcome = _run(clock, _TimedPark(clock), _StampedMount(clock), settle_ms=750)
        assert outcome.motion_ended_at == 0.0
        assert outcome.settled_at == 0.75
        assert clock.sleeps == [0.75]

    def test_the_runner_stays_busy_until_the_last_instant_of_the_settle(self) -> None:
        clock = FakeClock(auto_advance=False)
        runner = MountTestMoveRunner(clock=clock)
        runner.submit(
            _TimedPark(clock), _StampedMount(clock), *_A1P, 100, park_after=False, settle_ms=750
        )
        assert clock.wait_for_sleepers(1)
        clock.advance(0.5)
        assert runner.is_busy and runner.take_latest() is None
        clock.advance(0.25)  # exactly settle_ms
        outcome = _drain(runner)
        assert outcome.settled_at == 0.75 and outcome.pulsed is True


class TestRepark:
    def test_park_after_waits_for_the_park_confirmation(self, exact_polling: None) -> None:
        clock = FakeClock()
        park = _TimedPark(clock, park_delay_s=0.5)
        outcome = _run(clock, park, _StampedMount(clock), park_after=True)
        assert outcome.error is None and outcome.pulsed is True
        assert [name for name, _t in park.commands] == ["unpark", "park"]
        assert clock.monotonic() == 0.5

    def test_a_park_never_confirmed_is_reported_after_the_repark_deadline(
        self, exact_polling: None
    ) -> None:
        clock = FakeClock()
        park = _TimedPark(clock, park_delay_s=None)
        outcome = _run(clock, park, _StampedMount(clock), park_after=True)
        assert outcome.pulsed is True
        assert outcome.error == "mount did not confirm re-parked in time (not confirmed)"
        assert clock.monotonic() == 1.0


class TestStopInEachWaitState:
    """Stop (`abort()`) pressed while the worker is inside each wait. Pinned behaviour: a
    wait already in progress runs to its end (no wait takes the stop event -- S3b is
    behaviour-neutral; interruptible waits are S6.3's); the next step is never sent."""

    def test_stop_during_the_fresh_park_wait(self, exact_polling: None) -> None:
        clock = FakeClock()
        runner = MountTestMoveRunner(clock=clock)
        clock.call_at(0.3, runner.abort)
        mount = _StampedMount(clock)
        outcome = _run(clock, _TimedPark(clock, fresh_from=0.5), mount, runner=runner)
        assert outcome.error is not None and "stopped by the user" in outcome.error
        assert mount.attempted_at == []
        assert clock.monotonic() == 0.5  # the wait ran on until the fresh reading

    def test_stop_during_the_unpark_confirmation_wait(self, exact_polling: None) -> None:
        clock = FakeClock()
        runner = MountTestMoveRunner(clock=clock)
        clock.call_at(0.3, runner.abort)
        mount = _StampedMount(clock)
        outcome = _run(clock, _TimedPark(clock, unpark_delay_s=0.75), mount, runner=runner)
        assert outcome.pulsed is False
        assert outcome.error is not None and "stopped by the user" in outcome.error
        assert mount.attempted_at == []

    def test_stop_during_the_stop_tracking_gate_delay(self) -> None:
        clock = FakeClock()
        runner = MountTestMoveRunner(clock=clock)
        clock.call_at(0.1, runner.abort)
        mount = _StampedMount(clock)
        outcome = _run(clock, _TimedPark(clock, parked=False), mount, runner=runner)
        assert outcome.error is not None and "stopped by the user" in outcome.error
        assert mount.attempted_at == []
        assert clock.sleeps == [0.3]  # the gate delay itself ran to its end

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="discovered (S3b): _pulse_with_retry never checks Stop, so a rejected timed "
        "pulse is re-sent after the user pressed Stop -> S6.3/S6.6",
    )
    def test_stop_during_a_rejection_retry_delay_sends_no_further_attempt(self) -> None:
        clock = FakeClock()
        runner = MountTestMoveRunner(clock=clock)
        clock.call_at(0.1, runner.abort)
        mount = _StampedMount(clock, reject_first_n_pulses=1)
        _run(clock, _TimedPark(clock), mount, runner=runner)
        assert mount.attempted_at == [0.0]

    def test_stop_during_the_settle_lets_the_settle_finish(self) -> None:
        clock = FakeClock(auto_advance=False)
        runner = MountTestMoveRunner(clock=clock)
        runner.submit(
            _TimedPark(clock), _StampedMount(clock), *_A1P, 100, park_after=False, settle_ms=750
        )
        assert clock.wait_for_sleepers(1)
        runner.abort()
        clock.advance(0.0)
        assert runner.is_busy and clock.blocked_sleepers == 1  # not cut short
        clock.advance(0.75)
        outcome = _drain(runner)
        assert outcome.pulsed is True and outcome.error is None
        assert outcome.settled_at == 0.75

    def test_stop_during_the_repark_wait_still_waits_for_parked(self, exact_polling: None) -> None:
        clock = FakeClock()
        runner = MountTestMoveRunner(clock=clock)
        clock.call_at(0.25, runner.abort)
        park = _TimedPark(clock, park_delay_s=0.5)
        outcome = _run(clock, park, _StampedMount(clock), park_after=True, runner=runner)
        assert outcome.pulsed is True and outcome.error is None
        assert clock.monotonic() == 0.5  # the safe state was still confirmed
