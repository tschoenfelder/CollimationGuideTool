"""Issue #51: the production OnStep shims (`OnStepConnection`,
`OnStepFocuserAdapter`, `OnStepMountParkAdapter`, `OnStepMountPulseAdapter`)
over the scenario-configurable `SimulatedOnStepIndiClient`, on fake time.

`TestCompoundOperationSerialization` is the deterministic proof of 9cea2e9
(diagnostic 7b21bdf1): a status poll arriving from another thread while a
focuser move is in flight. Unlike the timing-based tests in
test_onstep_adapters.py, the move is held open on a manual `FakeClock`, the
poller is provably either blocked behind `operation_lock` or finished
(`ObservableRLock` barrier), and only then is the move released -- the same
schedule every run.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest
from astrotool_core.mount import AxisDirection, GuidePulseResult, MountAxis, MountStatus
from astrotool_core.onstep import (
    OnStepFocuserAdapter,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
)
from astrotool_core.testing import (
    ConnectFailure,
    FocuserScenario,
    GuidePulseScenario,
    OnStepScenario,
    install_observable_operation_lock,
    make_simulated_onstep_connection,
    simulate_onstep_adapter_041_package,
)
from astrotool_core.testing.fake_onstep_indi_client import (
    IndiRuntimeConfig,
    fake_indi_runtime_config,
)
from astrotool_core.testing.sim_onstep import AXIS_BUSY, SimulatedOnStepIndiClient
from astrotool_core.timing import FakeClock

_JOIN_S = 5.0  # real-time safety bound for joining test threads, never a policy wait


def _until(condition: Callable[[], bool], *, timeout_s: float = _JOIN_S) -> bool:
    """Barrier: True as soon as `condition()` holds (another thread got there); a real-time
    safety bound only, never a policy wait."""
    done = threading.Event()
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() >= deadline:
            return False
        done.wait(0.001)
    return True


class TestConnectErrorVariants:
    """0.4.1 `OnStepIndiClient.connect` (indi_client.py:126-210) raises
    TimeoutError (a property never arrived), ConnectionError (CONNECT not On),
    RuntimeError (already connected / Safe-flip or guard checks), ValueError
    (unparseable TIME_UTC) or KeyError (a missing UTC/East/West element), and
    closes its own transport first. `acquire()` must leave no half-open state
    for any of them (304178e) and a retry must work. KeyError is not in
    `acquire()`'s own except tuple: it propagates without `acquire()` calling
    close() -- still no half-open state, because the client closed itself."""

    @pytest.mark.parametrize(
        ("failure", "acquire_closes"),
        [
            (
                ConnectFailure(
                    TimeoutError("INDI property LX200 OnStep.CONNECTION did not update")
                ),
                1,
            ),
            (ConnectFailure(ConnectionError("OnStep INDI device is not connected")), 1),
            (ConnectFailure(RuntimeError("Safe HOME-route flip property is not ready")), 1),
            (ConnectFailure(ValueError("Invalid isoformat string: ''")), 1),
            # Raised before 0.4.1's try (:127-128): the client does NOT close itself,
            # so acquire()'s own close() is the only one.
            (
                ConnectFailure(
                    RuntimeError("INDI client is already connected; close before reconnecting"),
                    self_closes=False,
                ),
                1,
            ),
            # Not in acquire()'s except tuple: only 0.4.1's own self-close happens.
            (ConnectFailure(KeyError("UTC")), 0),
        ],
        ids=[
            "TimeoutError",
            "ConnectionError",
            "RuntimeError",
            "ValueError",
            "RuntimeError-already-connected",
            "KeyError",
        ],
    )
    def test_a_failed_connect_leaves_nothing_open_and_a_retry_succeeds(
        self, failure: ConnectFailure, acquire_closes: int
    ) -> None:
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(connect_errors=[failure]), clock=FakeClock()
        )
        focuser = OnStepFocuserAdapter(connection)
        with pytest.raises(type(failure.error)):
            focuser.connect()
        client = made[0]
        assert connection.client is None
        assert client.self_closes == (1 if failure.self_closes else 0)
        # OnStepConnection.acquire()'s own close(), counted apart from the self-close:
        assert client.close_calls == acquire_closes
        assert client.closed is True
        assert focuser.status().available is False

        focuser.connect()  # the fault has cleared
        assert focuser.status().available is True
        assert len(made) == 2

    def test_connect_latency_runs_on_fake_time(self) -> None:
        clock = FakeClock()
        connection, _ = make_simulated_onstep_connection(
            OnStepScenario(connect_latency_s=4.0), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        assert clock.monotonic() == 4.0


class TestParkAndTracking:
    def _park(self, scenario: OnStepScenario, clock: FakeClock) -> OnStepMountParkAdapter:
        connection, _ = make_simulated_onstep_connection(scenario, clock=clock)
        park = OnStepMountParkAdapter(connection)
        park.connect()
        return park

    def test_unpark_leaves_tracking_off_by_default(self) -> None:
        park = self._park(OnStepScenario(unpark_latency_s=1.5), clock := FakeClock())
        park.unpark()
        status = park.status()
        assert (status.parked, status.tracking) == (False, False)
        assert clock.monotonic() == 1.5

    def test_parked_mount_refuses_axis_motion_non_retryably(self) -> None:
        connection, made = make_simulated_onstep_connection(OnStepScenario(), clock=FakeClock())
        mount = OnStepMountPulseAdapter(connection)
        mount.connect()
        with pytest.raises(RuntimeError, match="fresh unparked"):
            made[0].mount.move_ra_axis_deg(0.1)
        assert made[0].axis_move_calls == []

    def test_tracking_not_confirmed_in_time_is_stopped_and_ends_off(self) -> None:
        """0.4.1 indi_tracking.py:115-117: an accepted TRACK_ON that fresh status
        does not confirm before the deadline ends in emergency_stop() -> OFF."""
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, tracking_confirm_delay_s=12.0), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        client = made[0]
        result = client.mount.enable_tracking()  # 0.4.1 default timeout: 8 s
        assert (result.command_accepted, result.tracking_confirmed) == (True, False)
        assert result.error == "Tracking ON was not confirmed by fresh OnStep status"
        # the TimeoutError comes at the first pre-poll check >= 8.0 s: 54 * 0.15
        assert clock.monotonic() == pytest.approx(54 * 0.15)
        assert client.emergency_stop_calls == 1
        clock.advance(60.0)
        assert client.tracking is False  # never turns on later

    def test_tracking_confirmed_on_the_second_poll_that_sees_it(self) -> None:
        """Tracking on at 3.0 s: poll 20 (3.0 s) sees it, poll 21 (3.15 s) confirms."""
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, tracking_confirm_delay_s=3.0), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        result = made[0].mount.enable_tracking()
        assert (result.command_accepted, result.tracking_confirmed) == (True, True)
        assert made[0].tracking is True
        assert clock.monotonic() == pytest.approx(3.15)

    @pytest.mark.parametrize(
        ("timeout", "confirmed", "ends_at"),
        [
            (3.01, True, 3.15),  # deadline just after the last pre-poll check (3.0 s)
            (3.0, False, 3.0),  # check exactly at the deadline: now >= deadline -> timeout
            (2.99, False, 3.0),  # deadline just before it
        ],
        ids=["just-after", "at", "just-before"],
    )
    def test_confirmation_deadline_boundary(
        self, timeout: float, confirmed: bool, ends_at: float
    ) -> None:
        """indi_tracking.py:89-92: the deadline is checked before each poll; the
        confirming poll (3.15 s) needs its pre-poll check (3.0 s) < deadline."""
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, tracking_confirm_delay_s=3.0), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        result = made[0].enable_tracking(timeout=timeout)  # client level: the facade has no timeout
        assert result.tracking_confirmed is confirmed
        assert made[0].tracking is confirmed
        assert made[0].emergency_stop_calls == (0 if confirmed else 1)
        assert clock.monotonic() == pytest.approx(ends_at)

    @pytest.mark.parametrize(
        "scenario",
        [
            {"at_home": True},
            {"home_authority": False},
            {"time_site_authority": False},
            {"status_stale": True},
            {"parked": True},
        ],
        ids=lambda s: next(iter(s)),
    )
    def test_strict_preflight_refuses_without_sending_track_on(
        self, scenario: dict[str, bool]
    ) -> None:
        """indi_tracking.py:57-74 (default policy "strict")."""
        clock = FakeClock()
        base: dict[str, bool] = {"parked": False}
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(**{**base, **scenario}),  # type: ignore[arg-type]
            clock=clock,
        )
        OnStepMountParkAdapter(connection).connect()
        result = made[0].mount.enable_tracking()
        assert (result.command_accepted, result.tracking_confirmed) == (False, False)
        assert made[0].tracking is False and clock.monotonic() == 0.0

    def test_already_tracking_is_not_a_new_command(self) -> None:
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, tracking=True), clock=FakeClock()
        )
        OnStepMountParkAdapter(connection).connect()
        result = made[0].mount.enable_tracking()
        assert (result.command_accepted, result.tracking_confirmed) == (False, True)


class TestAxisMoves:
    def test_a_move_takes_offset_over_rate_of_fake_time(self) -> None:
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, axis_rate_deg_per_s=0.5), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        made[0].mount.move_dec_axis_deg(1.0)
        assert clock.monotonic() == 2.0
        assert made[0].dec_deg == pytest.approx(21.0)

    def test_stale_status_refuses_the_move_with_0_4_1s_reason(self) -> None:
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, status_stale=True), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        snapshot = made[0].mount.get_status()
        assert snapshot.status_live is False
        assert "onstep_status_not_fresh" in snapshot.blockers
        with pytest.raises(RuntimeError, match="safety inputs unavailable"):
            made[0].mount.move_ra_axis_deg(0.1)

    def test_in_motion_failures_stop_the_mount_then_a_normal_move_works(self) -> None:
        """Non-retryable failures after the goto was issued: 0.4.1 calls
        emergency_stop() before re-raising (indi_axis_motion.py:246-249)."""
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(
                parked=False,
                axis_move_errors=[
                    RuntimeError("Axis move reversed or overran its finite target"),
                    TimeoutError("Axis move did not reach its finite target"),
                ],
            ),
            clock=clock,
        )
        OnStepMountParkAdapter(connection).connect()
        client = made[0]
        with pytest.raises(RuntimeError, match="overran"):
            client.mount.move_ra_axis_deg(0.1)
        with pytest.raises(TimeoutError):
            client.mount.move_ra_axis_deg(0.1)
        assert client.emergency_stop_calls == 2
        client.mount.move_ra_axis_deg(0.1)
        assert client.ha_deg == pytest.approx(-30.0 + 0.1)

    def test_a_move_while_another_is_active_is_refused_retryably(self) -> None:
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False), clock=FakeClock()
        )
        OnStepMountParkAdapter(connection).connect()
        client = made[0]
        # another axis motion holds 0.4.1's lock:
        with client._axis_lock, pytest.raises(RuntimeError, match=AXIS_BUSY):
            client.mount.move_ra_axis_deg(0.1)
        assert client.emergency_stop_calls == 0 and client.axis_move_calls == []

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"offset_deg": 20.0 / 3600.0}, "between 30 arcseconds"),
            ({"offset_deg": 0.1, "timeout_s": 0.5}, "timeout must be between"),
        ],
    )
    def test_0_4_1_argument_validation(self, kwargs: dict[str, float], message: str) -> None:
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False), clock=FakeClock()
        )
        OnStepMountParkAdapter(connection).connect()
        with pytest.raises(ValueError, match=message):
            made[0].mount.move_ra_axis_deg(**kwargs)

    def test_a_move_slower_than_its_timeout_times_out_and_stops(self) -> None:
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, axis_rate_deg_per_s=0.1), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        with pytest.raises(TimeoutError):
            made[0].mount.move_dec_axis_deg(5.0, timeout_s=30.0)
        assert clock.monotonic() == 30.0 and made[0].emergency_stop_calls == 1

    def test_angular_move_through_the_production_pulse_adapter(self) -> None:
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False), clock=clock
        )
        mount = OnStepMountPulseAdapter(connection)
        mount.connect()
        result = mount.move_angular(MountAxis.AXIS2, AxisDirection.NEGATIVE, 120.0)
        assert result.accepted, result.message
        ((axis, offset_deg),) = made[0].axis_move_calls
        assert axis == "dec"
        assert offset_deg * 3600.0 == pytest.approx(-120.0)
        assert clock.monotonic() > 0.0

    def test_a_stop_mid_move_stops_short_but_the_call_runs_to_its_own_deadline(self) -> None:
        """S6.0c, 0.4.1 source: ABORT stops the mount at once, but `move_*_axis_deg` has no
        cancel hook -- it polls on to its deadline, raises TimeoutError and stops again."""
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, axis_rate_deg_per_s=0.5), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        client = made[0]
        clock.call_at(1.0, client.emergency_stop)  # half-way through a 2 s move
        with pytest.raises(TimeoutError, match="did not reach its finite target"):
            client.mount.move_dec_axis_deg(1.0, timeout_s=30.0)
        assert clock.monotonic() == pytest.approx(30.0)
        assert client.dec_deg == pytest.approx(20.5)  # stopped half-way
        assert client.axis_moves_aborted == 1 and client.emergency_stop_calls == 2
        assert not client.slewing

    def test_a_stop_from_another_thread_while_the_move_blocks(self) -> None:
        clock = FakeClock(auto_advance=False)
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, axis_rate_deg_per_s=0.5, stop_confirm_latency_s=2.0),
            clock=clock,
        )
        OnStepMountParkAdapter(connection).connect()
        client = made[0]
        errors: list[BaseException] = []

        def move() -> None:
            try:
                client.mount.move_dec_axis_deg(1.0, timeout_s=30.0)
            except TimeoutError as exc:
                errors.append(exc)

        mover = threading.Thread(target=move, daemon=True)
        mover.start()
        assert clock.wait_for_sleepers(1)
        assert client.slewing
        stopper = threading.Thread(target=client.emergency_stop, daemon=True)
        stopper.start()
        # ABORT went out at once (the mover stops short at fake t=0); the stopper still waits
        # for its confirmation and the mover polls on to its deadline -- both on fake time
        assert _until(lambda: client.axis_moves_aborted == 1 and clock.blocked_sleepers == 2)
        assert not client.slewing and stopper.is_alive()
        assert client.dec_deg == pytest.approx(20.0)
        clock.advance(30.0)  # the deadline: TimeoutError, then 0.4.1's own second stop ...
        assert _until(lambda: client.emergency_stop_calls == 2 and clock.blocked_sleepers == 1)
        clock.advance(2.0)  # ... whose confirmation wait ends here
        mover.join(_JOIN_S)
        stopper.join(_JOIN_S)
        assert not mover.is_alive() and not stopper.is_alive()
        assert len(errors) == 1 and client.emergency_stop_calls == 2

    def test_emergency_stop_stops_tracking(self) -> None:
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, tracking=True), clock=clock
        )
        OnStepMountParkAdapter(connection).connect()
        assert made[0].mount.stop().stopped_confirmed is True
        assert made[0].tracking is False


class TestFocuser:
    def _focuser(self, clock: FakeClock, **scenario: object) -> tuple[OnStepFocuserAdapter, object]:
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(focuser=FocuserScenario(**scenario)),  # type: ignore[arg-type]
            clock=clock,
        )
        focuser = OnStepFocuserAdapter(connection)
        focuser.connect()
        return focuser, made[0].focuser

    def test_absolute_move_takes_fake_time_and_reports_position(self) -> None:
        clock = FakeClock()
        focuser, _ = self._focuser(clock, position=1000, steps_per_s=500.0)
        assert focuser.move_absolute(2000).accepted is True
        assert clock.monotonic() == 2.0
        assert focuser.get_position() == 2000

    def test_the_lower_of_driver_and_configured_maximum_limits_a_move(self) -> None:
        focuser, _ = self._focuser(FakeClock(), driver_maximum=10000, configured_maximum=8000)
        assert focuser.move_absolute(9000).accepted is False
        assert focuser.get_position() == 5000

    @pytest.mark.parametrize(
        ("scenario", "blocker"),
        [
            ({"position_stale": True}, "focuser_position_stale"),
            ({"configured_maximum": None}, "focuser_configured_maximum_missing"),
            ({"driver_maximum": None}, "focuser_maximum_unknown"),
        ],
    )
    def test_stale_or_missing_metadata_blocks_moves_and_is_reported(
        self, scenario: dict[str, object], blocker: str
    ) -> None:
        focuser, sim = self._focuser(FakeClock(), **scenario)
        assert blocker in focuser.blockers()
        assert focuser.move_absolute(5100).accepted is False
        assert sim.move_log == []  # type: ignore[attr-defined]

    def test_a_driver_rejection_is_not_accepted(self) -> None:
        focuser, _ = self._focuser(FakeClock(), reject_moves=True)
        assert focuser.move_absolute(5100).accepted is False
        assert focuser.get_position() == 5000

    def test_busy_forever_times_out_on_fake_time_and_stays_moving(self) -> None:
        clock = FakeClock()
        focuser, _ = self._focuser(clock, busy_forever=True)
        assert focuser.move_absolute(5100).accepted is False
        assert clock.monotonic() == 35.0  # 0.4.1: 30 s move timeout + 5 s stop wait
        assert focuser.status().moving is True
        focuser.stop()
        assert focuser.status().moving is True  # the abort was never confirmed

    def test_the_reported_position_moves_during_travel(self) -> None:
        clock = FakeClock()
        focuser, sim = self._focuser(clock, position=1000, steps_per_s=100.0)
        samples: list[tuple[int, bool]] = []
        clock.call_later(2.5, lambda: samples.append((focuser.get_position(), sim.moving)))  # type: ignore[attr-defined]
        assert focuser.move_absolute(1600).accepted is True
        assert samples == [(1250, True)]
        assert focuser.get_position() == 1600

    def test_a_non_integer_target_is_rejected_like_0_4_1(self) -> None:
        _, sim = self._focuser(FakeClock())
        with pytest.raises(ValueError, match="integer"):
            sim.move_absolute(5100.0)  # type: ignore[attr-defined]

    def test_backlash_lags_the_optics_after_a_direction_reversal(self) -> None:
        focuser, sim = self._focuser(FakeClock(), backlash_steps=40)
        focuser.move_absolute(5200)  # outward: optics trail by the play
        assert sim.optical_position == 5160  # type: ignore[attr-defined]
        focuser.move_absolute(5100)  # reversal: first 40 steps only take up the play
        assert sim.optical_position == 5100  # type: ignore[attr-defined]
        focuser.move_absolute(5300)
        assert sim.optical_position == 5260  # type: ignore[attr-defined]


class TestCompoundOperationSerialization:
    """9cea2e9 / diagnostic 7b21bdf1, deterministic scheduling (see module
    docstring). Fails with 9cea2e9 reverted: the poller then reads status in
    the middle of the move and the monitor records the interleaving."""

    def _run(self, poll: str) -> list[tuple[str, str]]:
        clock = FakeClock(auto_advance=False)
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(focuser=FocuserScenario(position=1000, steps_per_s=100.0)),
            clock=clock,
        )
        lock = install_observable_operation_lock(connection)
        focuser = OnStepFocuserAdapter(connection)
        park = OnStepMountParkAdapter(connection)
        focuser.connect()
        park.connect()
        monitor = made[0].monitor

        mover = threading.Thread(target=lambda: focuser.move_absolute(1600), daemon=True)
        mover.start()
        assert clock.wait_for_sleepers(1), "the move never started waiting"
        assert monitor.active == ("focuser.move",)

        poller = threading.Thread(
            target=(focuser.status if poll == "focuser" else park.status), daemon=True
        )
        poller.start()
        # Barrier: the poller is either provably queued behind the move
        # (fixed code) or already finished its read (no serialization).
        outcome = lock.wait_until_blocked_or_done(poller)
        assert outcome in ("blocked", "done"), outcome

        clock.advance(6.0)  # 600 steps at 100 steps/s: the move completes
        mover.join(_JOIN_S)
        poller.join(_JOIN_S)
        assert not mover.is_alive() and not poller.is_alive()
        return monitor.interleavings

    def test_a_status_poll_during_a_focuser_move_waits_for_the_move(self) -> None:
        assert self._run("focuser") == []

    def test_a_mount_status_poll_during_a_focuser_move_waits_for_the_move(self) -> None:
        assert self._run("park") == []


# ---------------------------------------------------------------------------
# S6.0d (#39): OnStepAdapter 0.5.0 guide pulses through the PRODUCTION
# `OnStepMountPulseAdapter`, on the simulator's 0.5.0 model (S6.0e: the pinned default; the
# published 0.4.1 is simulated explicitly -- no facade API, `simulate_onstep_adapter_041_package`).
# ---------------------------------------------------------------------------


def _guide_rig(
    monkeypatch: pytest.MonkeyPatch,
    *,
    api: bool = True,
    exports: bool = True,
    guide: GuidePulseScenario | None = None,
    config: IndiRuntimeConfig | None = None,
    **scenario: object,
) -> tuple[OnStepMountPulseAdapter, SimulatedOnStepIndiClient, FakeClock]:
    """Production adapter on the simulator; by default the pinned 0.5.0 (the installed package as
    is), tracking, unparked. `exports=False` hides 0.5.0's package additions (a 0.4.1 package);
    `api=False` gives the client the 0.4.1 facade (no `guide_pulse`)."""
    if not exports:
        simulate_onstep_adapter_041_package(monkeypatch)
    fields: dict[str, object] = {"parked": False, "tracking": True}
    fields.update(scenario)
    fields["guide_pulses"] = (guide or GuidePulseScenario()) if api else None
    clock = FakeClock()
    connection, made = make_simulated_onstep_connection(
        OnStepScenario(**fields),  # type: ignore[arg-type]
        clock=clock,
        config=config,
    )
    adapter = OnStepMountPulseAdapter(connection)
    adapter.connect()
    return adapter, made[0], clock


def _west(adapter: OnStepMountPulseAdapter, ms: int) -> GuidePulseResult:
    return adapter.guide_pulse(MountAxis.AXIS1, AxisDirection.POSITIVE, ms)


class TestGuidePulseCapability:
    """Detected once per connection from the installed OnStepAdapter: the facade's
    `guide_pulse` (0.5.0 indi_mount.py:32) AND the exported bounds (0.5.0 __init__.py).
    A separate capability -- `supports_pulse_guiding` (Mount Align's timed/angular choice)
    stays False either way."""

    def test_the_pinned_0_5_0_is_what_the_capable_tests_run_on(self) -> None:
        """The capable cases below use the installed package as is: it must be the pin
        (tests/contracts/test_onstep_guide_pulse_contract.py checks the pin itself)."""
        import onstep_adapter

        assert onstep_adapter.__version__ == "0.5.0"
        assert callable(getattr(onstep_adapter.IndiMount, "guide_pulse", None))

    def test_the_default_scenario_is_the_pinned_0_5_0(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connection, _made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, tracking=True), clock=FakeClock()
        )
        adapter = OnStepMountPulseAdapter(connection)
        adapter.connect()

        assert adapter.capabilities().supports_guide_pulses_while_tracking is True
        assert adapter.guide_pulse_range_ms == (20, 5000)

    def test_on_0_4_1_the_capability_is_absent_and_nothing_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, client, _clock = _guide_rig(monkeypatch, api=False, exports=False)

        caps = adapter.capabilities()
        result = _west(adapter, 500)

        assert caps.supports_guide_pulses_while_tracking is False
        assert caps.supports_pulse_guiding is False
        assert adapter.guide_pulse_range_ms is None
        assert result.accepted is False and result.sent is False
        assert "needs >= 0.5.0" in result.message
        assert client.guide_pulse_calls == []

    def test_with_0_5_0_the_capability_is_reported_separately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, _client, _clock = _guide_rig(monkeypatch)

        caps = adapter.capabilities()

        assert caps.supports_guide_pulses_while_tracking is True
        assert caps.supports_pulse_guiding is False  # Mount Align's choice is unaffected
        assert adapter.guide_pulse_range_ms == (20, 5000)

    def test_a_facade_without_exported_bounds_is_not_capable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, _client, _clock = _guide_rig(monkeypatch, exports=False)

        assert adapter.capabilities().supports_guide_pulses_while_tracking is False

    def test_exported_bounds_without_the_facade_api_are_not_capable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The installed 0.5.0 package's exports alone do not make a 0.4.1 facade capable."""
        adapter, client, _clock = _guide_rig(monkeypatch, api=False)

        assert adapter.capabilities().supports_guide_pulses_while_tracking is False
        assert "needs >= 0.5.0" in _west(adapter, 500).message
        assert client.guide_pulse_calls == []

    def test_disconnected_means_no_capability(self, monkeypatch: pytest.MonkeyPatch) -> None:
        adapter, _client, _clock = _guide_rig(monkeypatch)
        adapter.disconnect()

        assert adapter.capabilities().supports_guide_pulses_while_tracking is False
        assert _west(adapter, 500).message == "not connected"


class TestGuidePulseExecution:
    def test_a_pulse_is_chunked_and_keeps_tracking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0.5.0 indi_guiding.py:156-164: 500 ms chunks, never a remainder below 20 ms."""
        adapter, client, clock = _guide_rig(monkeypatch)
        ha0 = client.ha_deg

        result = _west(adapter, 1200)
        short = _west(adapter, 520)

        assert result.accepted and result.sent and result.tracking_preserved
        assert (result.chunks_requested, result.chunks_completed) == (3, 3)
        assert client.guide_chunks_issued == [
            ("west", 500), ("west", 500), ("west", 200), ("west", 500), ("west", 20),
        ]
        assert short.accepted
        assert client.tracking is True and client.emergency_stop_calls == 0
        assert client.tracking_enable_calls == 0  # never touches tracking (#44)
        assert clock.monotonic() == pytest.approx(1.72)
        assert client.ha_deg > ha0  # AXIS1 POSITIVE = west = increasing hour angle

    @pytest.mark.parametrize(
        ("axis", "direction", "expected"),
        [
            (MountAxis.AXIS1, AxisDirection.POSITIVE, "west"),
            (MountAxis.AXIS1, AxisDirection.NEGATIVE, "east"),
            (MountAxis.AXIS2, AxisDirection.POSITIVE, "north"),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE, "south"),
        ],
    )
    def test_axis_directions_map_to_guide_directions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        axis: MountAxis,
        direction: AxisDirection,
        expected: str,
    ) -> None:
        adapter, client, _clock = _guide_rig(monkeypatch)

        assert adapter.guide_pulse(axis, direction, 100).accepted
        assert client.guide_pulse_calls == [(expected, 100)]

    @pytest.mark.parametrize("ms", [19, 5001, 0])
    def test_a_duration_outside_0_5_0_bounds_is_refused_unsent(
        self, monkeypatch: pytest.MonkeyPatch, ms: int
    ) -> None:
        adapter, client, _clock = _guide_rig(monkeypatch)

        result = _west(adapter, ms)

        assert not result.accepted and not result.sent
        assert "20-5000 ms" in result.message
        assert client.guide_pulse_calls == []

    def test_warnings_are_surfaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0.5.0 allows a pulse during a flip warning (strict phases include flip_required,
        indi_guiding.py:47-50) and reports `meridian_flip_required` (:110-111)."""
        adapter, _client, _clock = _guide_rig(
            monkeypatch, guide=GuidePulseScenario(meridian_phase="flip_required")
        )

        result = _west(adapter, 300)

        assert result.accepted
        assert result.warnings == ("meridian_flip_required",)

    def test_controller_managed_authority_gaps_are_warnings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, _client, _clock = _guide_rig(
            monkeypatch,
            config=fake_indi_runtime_config(tracking_authority_policy="controller_managed"),
            home_authority=False,
        )

        result = _west(adapter, 300)

        assert result.accepted
        assert result.warnings == ("home_authority_unestablished",)

    def test_status_during_a_guide_pulse_does_not_claim_a_slew(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, _client, clock = _guide_rig(monkeypatch)
        assert adapter.status().tracking  # a reading to hold over
        seen: list[MountStatus] = []

        def poll_from_the_gui_thread() -> None:
            poller = threading.Thread(target=lambda: seen.append(adapter.status()))
            poller.start()
            poller.join(_JOIN_S)

        clock.call_later(0.2, poll_from_the_gui_thread)
        assert _west(adapter, 1000).accepted

        assert len(seen) == 1
        assert seen[0].connected and seen[0].tracking and not seen[0].slewing


#: Every refusal class of 0.5.0's preflight (indi_guiding.py:102-137) and its lock (:166-170).
_REFUSALS = [
    pytest.param(
        {"guide": GuidePulseScenario(extra_blockers=("onstep_status_alert",))},
        "guide safety inputs unavailable: ['onstep_status_alert']",
        id="hard-blocker",
    ),
    pytest.param(
        {"status_stale": True},
        "guide safety inputs unavailable: ['onstep_status_not_fresh']",
        id="stale-status",
    ),
    pytest.param(
        {"at_limit": True},
        "guide safety inputs unavailable: ['onstep_limit_or_park_fault']",
        id="firmware-limit",
    ),
    pytest.param(
        {"tracking": False},
        "guide pulse requires fresh unparked tracking state with no slew, HOME, fault or limit",
        id="not-tracking",
    ),
    pytest.param(
        {"parked": True},
        "guide pulse requires fresh unparked tracking state with no slew, HOME, fault or limit",
        id="parked",
    ),
    pytest.param(
        {"at_home": True},
        "guide pulse requires fresh unparked tracking state with no slew, HOME, fault or limit",
        id="at-home",
    ),
    pytest.param(
        {"slewing": True},
        "guide pulse requires fresh unparked tracking state with no slew, HOME, fault or limit",
        id="slewing",
    ),
    pytest.param(
        {"guide": GuidePulseScenario(meridian_phase="hard_stop")},
        "guide pulse refused at meridian phase hard_stop",
        id="meridian-hard-stop",
    ),
    pytest.param(
        {"home_authority": False},
        "guide astronomical authority unavailable: ['home_authority_unestablished'] "
        "phase=pre_meridian_allowed",
        id="strict-no-home-authority",
    ),
    pytest.param(
        {"time_site_authority": False},
        "guide astronomical authority unavailable: ['hour_angle_unavailable', "
        "'time_site_authority_unestablished'] phase=unknown",
        id="strict-no-time-site-authority",
    ),
    pytest.param(
        {"guide": GuidePulseScenario(extra_blockers=("pier_side_unknown",))},
        "guide astronomical authority unavailable: ['pier_side_unknown'] "
        "phase=pre_meridian_allowed",
        id="strict-pier-side-unknown",
    ),
    pytest.param({"lock_held": True}, "another guide pulse is active", id="concurrent-pulse"),
]


class TestGuidePulseRefusals:
    @pytest.mark.parametrize(("setup", "expected"), _REFUSALS)
    def test_each_refusal_is_surfaced_and_nothing_is_sent(
        self, monkeypatch: pytest.MonkeyPatch, setup: dict[str, object], expected: str
    ) -> None:
        fields = dict(setup)
        guide = fields.pop("guide", None)
        lock_held = bool(fields.pop("lock_held", False))
        slewing = bool(fields.pop("slewing", False))
        at_limit = bool(fields.pop("at_limit", False))
        adapter, client, _clock = _guide_rig(
            monkeypatch, guide=guide, **fields  # type: ignore[arg-type]
        )
        client.slewing, client.at_limit = slewing, at_limit
        if lock_held:
            client._guide_lock.acquire()

        result = _west(adapter, 1000)

        assert result.accepted is False and result.sent is False
        assert result.message == expected
        assert result.tracking_off is False
        assert client.guide_chunks_issued == []
        assert client.emergency_stop_calls == 0

    def test_a_client_that_is_not_connected_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, client, _clock = _guide_rig(monkeypatch)
        client.closed = True  # 0.5.0 indi_client.py:246-247 raises ConnectionError

        result = _west(adapter, 100)

        assert not result.accepted and result.message == "INDI client is not connected"


class TestGuidePulseFailureAfterAnIssuedChunk:
    """0.5.0 indi_guiding.py:241-258: a failure once a chunk was issued triggers
    `emergency_stop()` (ABORT + TRACK_OFF). Its `tracking_preserved` still comes from the
    snapshot BEFORE that stop (:252-254), so the adapter reads the status again and reports
    `tracking_off` -- prominently, in the message too."""

    def test_a_chunk_timeout_stops_the_mount_and_says_tracking_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guide = GuidePulseScenario(
            chunk_errors=[
                None, TimeoutError("INDI guide pulse TELESCOPE_TIMED_GUIDE_WE did not complete")
            ]
        )
        adapter, client, _clock = _guide_rig(monkeypatch, guide=guide)

        result = _west(adapter, 1500)

        assert client.emergency_stop_calls == 1 and client.tracking is False
        assert result.accepted is False and result.sent is True
        assert result.tracking_preserved is True  # 0.5.0's pre-stop snapshot
        assert result.tracking_off is True
        assert (result.chunks_requested, result.chunks_completed) == (3, 1)
        assert "did not complete" in result.message
        assert "NOT tracking" in result.message

    def test_a_refusal_after_the_last_chunk_also_stops_the_mount(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final preflight's refusal is raised (indi_guiding.py:206-207) -> emergency stop."""
        adapter, client, clock = _guide_rig(monkeypatch)
        clock.call_later(0.9, lambda: setattr(client, "meridian_phase", "hard_stop"))

        result = _west(adapter, 1000)

        assert client.emergency_stop_calls == 1
        assert result.tracking_off is True
        assert result.message.startswith("guide pulse refused at meridian phase hard_stop")

    def test_a_refusal_arising_during_a_chunk_stops_the_mount(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S6.0e, the PUBLISHED 0.5.0 (the S6.0d pre-release returned here without a stop):
        after every chunk it waits for a fresh OnStep status and re-runs the preflight
        (indi_guiding.py:221-227, :282-296); a refusal there is raised (`_GuideSafetyError`) ->
        emergency stop, and that chunk is not counted as completed."""
        adapter, client, clock = _guide_rig(monkeypatch)
        clock.call_later(0.2, lambda: setattr(client, "meridian_phase", "hard_stop"))

        result = _west(adapter, 1500)

        assert client.guide_chunks_issued == [("west", 500)]
        assert client.emergency_stop_calls == 1 and client.tracking is False
        assert result.sent is True and result.accepted is False
        assert result.tracking_off is True
        assert (result.chunks_requested, result.chunks_completed) == (3, 0)
        assert result.message.startswith("guide pulse refused at meridian phase hard_stop")
        assert "NOT tracking" in result.message

    def test_a_guide_flag_that_never_clears_stops_the_mount(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S6.0e, published 0.5.0: no fresh status with the `:GU#` `G` flag cleared within the
        command timeout after a chunk -> TimeoutError (indi_guiding.py:298-302) -> emergency
        stop; the adapter reports tracking OFF."""
        guide = GuidePulseScenario(idle_timeouts=[False, True])
        adapter, client, clock = _guide_rig(monkeypatch, guide=guide)

        result = _west(adapter, 1500)

        assert client.guide_chunks_issued == [("west", 500), ("west", 500)]
        assert client.emergency_stop_calls == 1 and client.tracking is False
        assert (result.chunks_requested, result.chunks_completed) == (3, 1)
        assert result.sent is True and result.tracking_off is True
        assert result.message.startswith(
            "OnStep still reports guide pulse active or did not publish a fresh post-guide status"
        )
        assert clock.monotonic() == pytest.approx(1.0 + 3.0)  # 2 chunks + the 3 s wait


class TestStopDuringAGuidePulse:
    """S6.0c semantics for guide pulses: Stop is lock-free and sends OnStep's emergency stop
    (ABORT + TRACK_OFF) while the pulse holds `operation_lock`; 0.5.0's next chunk preflight then
    refuses (not tracking) -- no further chunk is sent. A latched Stop refuses the next pulse
    unsent until the operator's next command re-arms."""

    def test_stop_mid_pulse_ends_it_and_reports_tracking_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, client, clock = _guide_rig(monkeypatch)

        def press_stop() -> None:
            adapter.abort()  # returns at once (S6.0c)
            # Barrier: the stop worker's emergency stop has reached the controller.
            assert _until(lambda: not client.tracking), "the stop never reached the mount"

        clock.call_later(0.3, press_stop)
        result = _west(adapter, 2000)
        worker = adapter._stop_worker
        if worker is not None:
            worker.join(_JOIN_S)

        assert client.guide_chunks_issued == [("west", 500)]  # nothing after the Stop
        assert result.accepted is False and result.sent is True
        assert result.tracking_off is True
        assert result.message.startswith("stopped by the user")
        # Review fix 2: attributed to the user's Stop, not to a failed pulse.
        assert "when a sent guide pulse fails" not in result.message
        assert "Stop" in result.message and "NOT tracking" in result.message

    def test_a_latched_stop_refuses_the_next_pulse_unsent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        adapter, client, _clock = _guide_rig(monkeypatch)
        adapter.abort()
        worker = adapter._stop_worker
        if worker is not None:
            worker.join(_JOIN_S)
        client.tracking = True  # the operator re-enabled tracking

        refused = _west(adapter, 500)
        adapter.clear_abort()
        accepted = _west(adapter, 500)

        assert refused.message == "stopped by the user -- guide pulse not sent"
        assert not refused.sent
        assert accepted.accepted

    def test_a_latched_stop_says_when_the_stop_ended_tracking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review fix 1: a Stop between pulses sends 0.5.0's emergency stop (ABORT + TRACK_OFF);
        the next pulse is refused unsent -- and the user is told the mount no longer tracks."""
        adapter, client, _clock = _guide_rig(monkeypatch)
        adapter.abort()
        worker = adapter._stop_worker
        if worker is not None:
            worker.join(_JOIN_S)
        assert client.tracking is False

        refused = _west(adapter, 500)

        assert not refused.sent and not refused.accepted
        assert refused.tracking_off is True
        assert refused.message.startswith("stopped by the user -- guide pulse not sent")
        assert "NOT tracking" in refused.message
        assert client.guide_pulse_calls == []


class TestGuidePulseTrackingUnknown:
    """Review fix 3: unknown means possibly stopped -- 'tracking may be OFF, check the mount'."""

    def test_a_failed_status_read_after_a_failed_pulse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        guide = GuidePulseScenario(
            chunk_errors=[RuntimeError("INDI rejected guide pulse TELESCOPE_TIMED_GUIDE_WE")]
        )
        adapter, client, _clock = _guide_rig(monkeypatch, guide=guide)

        def unreadable() -> object:
            raise ConnectionError("INDI status unavailable")

        monkeypatch.setattr(client.mount, "get_status", unreadable)

        result = _west(adapter, 300)

        assert result.sent and not result.accepted
        assert result.tracking_off is True
        assert "tracking may be OFF" in result.message and "check the mount" in result.message

    def test_an_unconfirmed_emergency_stop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0.5.0 appends '; emergency stop failed: ...' (indi_guiding.py:217-219)."""
        guide = GuidePulseScenario(
            chunk_errors=[
                TimeoutError("INDI guide pulse TELESCOPE_TIMED_GUIDE_WE did not complete")
            ]
        )
        adapter, client, _clock = _guide_rig(monkeypatch, guide=guide)

        def failing_stop(*, timeout: float = 5.0) -> object:
            raise RuntimeError("abort not acknowledged")

        monkeypatch.setattr(client, "emergency_stop", failing_stop)

        result = _west(adapter, 300)

        assert "emergency stop failed" in result.message
        assert result.tracking_off is True
        assert "tracking may be OFF" in result.message

