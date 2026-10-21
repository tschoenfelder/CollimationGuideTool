"""MountTestMoveRunner on the production contract (S6.8d, R9).

The runner's timed path (`pulse_axis` at a rate preset, rejection retries, the timed fallback
after an at-home refusal, `park_after=True` re-parking, the one-step `submit`) is deleted; the
tests that pinned it went with it. Every axis step here is an angular `(axis, direction, ms,
arcsec)` step on `FakeAngularMountAdapter`, the production-shaped fake."""

import time
from collections.abc import Callable

from astrotool_core.mount.park_port import MountParkPort, MountParkStatus
from astrotool_core.mount.port import AxisDirection, CommandResult, MountAxis
from astrotool_core.testing.fake_mount import FakeAngularMountAdapter, FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.ui.mount_test_move_runner import (
    MOTION_ANGULAR,
    MountPulseOutcome,
    MountTestMoveRunner,
)

_RA_PLUS = (MountAxis.AXIS1, AxisDirection.POSITIVE, 500, 60.0)
_DEC_PLUS = (MountAxis.AXIS2, AxisDirection.POSITIVE, 100, 12.0)


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _mount(**kwargs: object) -> FakeAngularMountAdapter:
    mount = FakeAngularMountAdapter(**kwargs)  # type: ignore[arg-type]
    mount.connect()
    return mount


class _NeverUnparks(MountParkPort):
    """unpark() is accepted but never settles -- a hung mount must not hang this runner."""

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return True

    def status(self) -> MountParkStatus:
        return MountParkStatus(available=True, parked=True, tracking=False)

    def park(self) -> None:
        pass

    def unpark(self) -> None:
        pass

    def stop_tracking(self) -> None:
        pass

    def start_tracking(self) -> None:
        pass


class TestMountTestMoveRunner:
    def test_a_sequence_unparks_moves_and_never_reparks(self) -> None:
        mount_park = FakeMountPark(start_parked=True)
        mount = _mount()
        runner = MountTestMoveRunner()

        assert runner.submit_sequence(mount_park, mount, [_RA_PLUS]) is True

        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None and outcome.pulsed is True and outcome.error is None
        assert outcome.motion_paths == (MOTION_ANGULAR,)
        assert mount.angular_log == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 60.0)]
        assert mount_park.status().parked is False  # S6.8d: no re-park at all

    def test_take_latest_returns_none_when_nothing_has_completed_yet(self) -> None:
        assert MountTestMoveRunner().take_latest() is None

    def test_take_latest_clears_the_outcome_so_it_is_returned_only_once(self) -> None:
        runner = MountTestMoveRunner()
        runner.submit_sequence(FakeMountPark(start_parked=True), _mount(), [_RA_PLUS])
        assert _wait_for(lambda: not runner.is_busy)
        assert runner.take_latest() is not None
        assert runner.take_latest() is None

    def test_a_submit_while_busy_is_a_no_op(self) -> None:
        runner = MountTestMoveRunner()
        runner._busy = True  # noqa: SLF001 -- simulate an in-flight move
        assert runner.submit_sequence(FakeMountPark(), _mount(), [_RA_PLUS]) is False

    def test_a_refused_move_reports_the_adapters_reason(self) -> None:
        mount = _mount(refuse_angular="refused: axis_motion_reached_hard_limit")
        runner = MountTestMoveRunner()
        runner.submit_sequence(FakeMountPark(start_parked=False), mount, [_RA_PLUS])
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None and outcome.pulsed is False
        assert outcome.error is not None and "hard_limit" in outcome.error
        assert mount.angular_log == []

    def test_a_sequence_runs_every_step_back_to_back_after_one_unpark(self) -> None:
        mount_park = FakeMountPark(start_parked=True)
        mount = _mount()
        runner = MountTestMoveRunner()

        runner.submit_sequence(mount_park, mount, [_RA_PLUS, _DEC_PLUS])

        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None and outcome.pulsed is True
        assert mount.angular_log == [
            (MountAxis.AXIS1, AxisDirection.POSITIVE, 60.0),
            (MountAxis.AXIS2, AxisDirection.POSITIVE, 12.0),
        ]
        assert mount_park.unpark_count == 1

    def test_a_second_submit_on_an_already_unparked_mount_does_not_re_unpark(self) -> None:
        """Real live-hardware report ("You seem to enable tracking ... no wonder the frames look
        blury"): unpark() on every submit re-triggered OnStep's delayed auto-tracking-on quirk.
        Only a mount that is parked right now gets unpark(); S6.8a: no stop_tracking() either."""
        mount_park = FakeMountPark(start_parked=True)
        mount = _mount()
        runner = MountTestMoveRunner()

        runner.submit_sequence(mount_park, mount, [_RA_PLUS])
        assert _wait_for(lambda: not runner.is_busy)
        runner.submit_sequence(mount_park, mount, [_DEC_PLUS])
        assert _wait_for(lambda: not runner.is_busy)

        outcome = runner.take_latest()
        assert outcome is not None and outcome.pulsed is True
        assert mount_park.unpark_count == 1
        assert mount_park.stop_tracking_count == 0

    def test_a_submit_after_the_mount_was_parked_elsewhere_unparks_again(self) -> None:
        """The parked check is live: a park in between (the Mount panel) means the next submit
        goes through the unpark cycle again."""
        mount_park = FakeMountPark(start_parked=True)
        mount = _mount()
        runner = MountTestMoveRunner()

        runner.submit_sequence(mount_park, mount, [_RA_PLUS])
        assert _wait_for(lambda: not runner.is_busy)
        mount_park.park()
        runner.submit_sequence(mount_park, mount, [_DEC_PLUS])
        assert _wait_for(lambda: not runner.is_busy)

        assert mount_park.unpark_count == 2
        assert mount_park.stop_tracking_count == 0

    def test_a_sequence_with_no_steps_is_a_no_op(self) -> None:
        runner = MountTestMoveRunner()
        assert runner.submit_sequence(FakeMountPark(start_parked=True), _mount(), []) is False
        assert runner.is_busy is False

    def test_a_mount_that_never_unparks_times_out_instead_of_hanging(self) -> None:
        mount = _mount()
        runner = MountTestMoveRunner()
        import collimation_tool.ui.mount_test_move_runner as runner_module

        original_timeout = runner_module._UNPARK_TIMEOUT_S
        runner_module._UNPARK_TIMEOUT_S = 0.1
        try:
            runner.submit_sequence(_NeverUnparks(), mount, [_RA_PLUS])
            assert _wait_for(lambda: not runner.is_busy, timeout_s=2.0)
        finally:
            runner_module._UNPARK_TIMEOUT_S = original_timeout
        outcome = runner.take_latest()
        assert outcome is not None and outcome.pulsed is False
        assert outcome.error is not None and "unpark" in outcome.error
        assert mount.angular_log == []

    def test_settle_ms_delays_completion_after_a_successful_move(self) -> None:
        """Real report "calibration doesn't wait for mount to be stabilized": the runner stays
        busy for settle_ms after the last move succeeded."""
        runner = MountTestMoveRunner()
        started_at = time.monotonic()
        runner.submit_sequence(
            FakeMountPark(start_parked=True), _mount(), [_RA_PLUS], settle_ms=100
        )
        assert _wait_for(lambda: not runner.is_busy, timeout_s=2.0)
        outcome = runner.take_latest()
        assert outcome is not None and outcome.pulsed is True
        assert time.monotonic() - started_at >= 0.1

    def test_settle_ms_is_skipped_when_the_move_is_refused(self) -> None:
        runner = MountTestMoveRunner()
        started_at = time.monotonic()
        runner.submit_sequence(
            FakeMountPark(start_parked=True),
            _mount(refuse_angular="refused"),
            [_RA_PLUS],
            settle_ms=5000,
        )
        assert _wait_for(lambda: not runner.is_busy, timeout_s=2.0)
        outcome = runner.take_latest()
        assert outcome is not None and outcome.pulsed is False
        assert time.monotonic() - started_at < 2.0


class TestAxisStepsAreAngularOnly:
    """S6.8d (R9): an axis step is an angular move or nothing -- no timed pulse, no fallback."""

    def _run(self, mount: FakeMountAdapter, steps: list) -> MountPulseOutcome:  # type: ignore[type-arg]
        runner = MountTestMoveRunner()
        assert runner.submit_sequence(FakeMountPark(start_parked=False), mount, steps)
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        return outcome

    def test_an_at_home_refusal_is_final_and_never_falls_back_to_a_timed_move(self) -> None:
        mount = _mount(refuse_angular="refused: axis_motion_refused_at_home")
        outcome = self._run(mount, [_RA_PLUS])
        assert not outcome.pulsed
        assert outcome.error is not None and "axis_motion_refused_at_home" in outcome.error
        assert mount.pulse_log == [] and mount.angular_log == []

    def test_a_step_without_an_angular_size_is_refused_and_nothing_is_sent(self) -> None:
        mount = _mount()
        outcome = self._run(mount, [(MountAxis.AXIS1, AxisDirection.POSITIVE, 400)])
        assert not outcome.pulsed
        assert outcome.error is not None and "no angular" in outcome.error
        assert mount.pulse_log == [] and mount.angular_log == []

    def test_a_mount_without_angular_moves_is_refused_and_never_pulsed(self) -> None:
        mount = FakeMountAdapter()  # timed pulses only (the dormant GuideTool's fake)
        mount.connect()
        outcome = self._run(mount, [_RA_PLUS])
        assert not outcome.pulsed
        assert outcome.error is not None and "no angular" in outcome.error
        assert mount.pulse_log == []


class _RaisingPark(FakeMountPark):
    """Issue #49: any exception in the worker used to kill the thread WITHOUT clearing
    `_busy` -- the runner then stayed busy forever and the panel looked frozen."""

    def __init__(self) -> None:
        super().__init__(start_parked=True)

    def unpark(self) -> None:
        raise ConnectionError("indiserver went away during unpark")


class _RaisingMount(FakeAngularMountAdapter):
    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        raise RuntimeError("driver crashed mid-move")


class TestWorkerCrashesNeverStrandTheRunner:
    def _finished_outcome(self, runner: MountTestMoveRunner) -> MountPulseOutcome:
        assert _wait_for(lambda: not runner.is_busy), "runner stayed busy after a worker crash"
        outcome = runner.take_latest()
        assert outcome is not None, "a crash must still publish an outcome"
        return outcome

    def test_an_exception_in_unpark_is_reported_and_clears_busy(self) -> None:
        runner = MountTestMoveRunner()
        assert runner.submit_sequence(_RaisingPark(), _mount(), [_RA_PLUS])
        outcome = self._finished_outcome(runner)
        assert outcome.pulsed is False
        assert outcome.error is not None and "worker crashed" in outcome.error
        assert "unpark" in outcome.error

    def test_an_exception_in_the_move_is_reported_and_clears_busy(self) -> None:
        runner = MountTestMoveRunner()
        mount = _RaisingMount()
        mount.connect()
        assert runner.submit_sequence(FakeMountPark(start_parked=False), mount, [_RA_PLUS])
        outcome = self._finished_outcome(runner)
        assert outcome.pulsed is False
        assert outcome.error is not None and "driver crashed mid-move" in outcome.error

    def test_the_runner_is_usable_again_after_a_crash(self) -> None:
        runner = MountTestMoveRunner()
        mount = _mount()
        assert runner.submit_sequence(_RaisingPark(), mount, [_RA_PLUS])
        self._finished_outcome(runner)

        assert runner.submit_sequence(FakeMountPark(start_parked=False), mount, [_RA_PLUS])
        outcome = self._finished_outcome(runner)
        assert outcome.pulsed is True and outcome.error is None
        assert mount.angular_log


class TestOneMotionInFlightAppWide:
    """S6.8a (R3): while another workflow moves the mount the request is answered at once with
    the busy reason -- nothing sent, nothing queued, not counted as an accepted submit."""

    def test_a_submit_while_another_workflow_moves_is_refused_with_the_reason(self) -> None:
        from collimation_tool.application.mount_motion import MountMotionExecutor

        mount = _mount()
        executor = MountMotionExecutor(mount)
        other = executor.claim("guide-assisted reacquisition")
        assert not isinstance(other, str)
        runner = MountTestMoveRunner(executor=executor)

        assert runner.submit_sequence(FakeMountPark(start_parked=False), mount, [_RA_PLUS])

        assert runner.is_busy is False
        outcome = runner.take_latest()
        assert outcome is not None and outcome.pulsed is False
        assert outcome.error is not None and "mount busy" in outcome.error
        assert "guide-assisted reacquisition" in outcome.error
        assert mount.angular_log == []
        assert runner.submit_count == 0
