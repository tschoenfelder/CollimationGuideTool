"""Issue #44: one explicit, mode-aware tracking policy.

TERRESTRIAL => mount tracking required OFF (fail closed if it cannot be
turned off); ASTRONOMICAL => the policy never forces tracking."""

from __future__ import annotations

import threading
from collections.abc import Callable

import pytest
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer, TrackingGate
from astrotool_core.mount.park_port import MountParkStatus
from astrotool_core.mount.tracking_mode import (
    MOUNT_BUSY_REASON,
    TrackingMode,
    TrackingVerificationResult,
    TrackingVerificationStatus,
    ensure_tracking_mode,
)
from astrotool_core.onstep import OnStepMountParkAdapter
from astrotool_core.testing import (
    install_observable_operation_lock,
    make_simulated_onstep_connection,
)
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.sim_onstep import OnStepScenario

_JOIN_S = 5.0  # real-time safety bound for test threads, never a policy wait


def _enforcer(mount: FakeMountPark, mode: OperatingMode) -> TrackingEnforcer:
    return TrackingEnforcer(mount, mode, settle_timeout_s=0.02)


def _tracking_mount(*, refuse_stop: bool = False) -> FakeMountPark:
    mount = FakeMountPark(start_parked=False, refuse_stop_tracking=refuse_stop)
    mount.start_tracking()
    return mount


class TestTerrestrialForcesTrackingOff:
    def test_entering_terrestrial_mode_with_tracking_on_turns_it_off(self) -> None:
        mount = _tracking_mount()
        enforcer = _enforcer(mount, OperatingMode.ASTRONOMICAL)

        gate = enforcer.set_mode(OperatingMode.TERRESTRIAL)

        assert gate.allowed
        assert mount.status().tracking is False

    def test_connect_with_tracking_on_is_turned_off_before_measurement(self) -> None:
        mount = _tracking_mount()
        enforcer = _enforcer(mount, OperatingMode.TERRESTRIAL)

        gate = enforcer.enforce("connect")

        assert gate.allowed
        assert mount.status().tracking is False
        assert mount.stop_tracking_count == 1

    def test_tracking_that_cannot_be_disabled_fails_closed_with_a_reason(self) -> None:
        mount = _tracking_mount(refuse_stop=True)
        enforcer = _enforcer(mount, OperatingMode.TERRESTRIAL)

        gate = enforcer.enforce("calibration")

        assert not gate.allowed
        assert "tracking" in gate.reason.lower()
        assert "could not be disabled" in gate.reason.lower()
        assert enforcer.measurement_allowed() is False

    def test_the_enforcer_never_enables_tracking_in_terrestrial_mode(self) -> None:
        mount = FakeMountPark(start_parked=False)
        enforcer = _enforcer(mount, OperatingMode.TERRESTRIAL)

        for context in ("connect", "calibration", "nudge", "fov", "autofocus", "reconnect"):
            enforcer.enforce(context)

        assert mount.start_tracking_count == 0
        assert mount.status().tracking is False

    def test_tracking_reenabled_by_a_movement_side_effect_is_turned_off_again(self) -> None:
        mount = FakeMountPark(start_parked=False)
        enforcer = _enforcer(mount, OperatingMode.TERRESTRIAL)
        assert enforcer.enforce("before_move").allowed

        mount.simulate_driver_reenabled_tracking()  # driver side effect of a slew/pulse
        gate = enforcer.enforce("after_move")

        assert gate.allowed
        assert mount.status().tracking is False

    def test_unavailable_mount_has_nothing_to_enforce(self) -> None:
        enforcer = _enforcer(FakeMountPark(available=False), OperatingMode.TERRESTRIAL)

        gate = enforcer.enforce("connect")

        assert gate.allowed
        assert "unavailable" in gate.reason.lower()


class TestModeTransitions:
    def test_astronomical_mode_does_not_force_tracking(self) -> None:
        mount = FakeMountPark(start_parked=False)
        enforcer = _enforcer(mount, OperatingMode.ASTRONOMICAL)

        gate = enforcer.enforce("calibration")

        assert gate.allowed
        assert mount.stop_tracking_count == 0
        assert mount.start_tracking_count == 0

    def test_switching_terrestrial_to_astronomical_permits_astronomical_tracking(self) -> None:
        mount = FakeMountPark(start_parked=False)
        enforcer = _enforcer(mount, OperatingMode.TERRESTRIAL)
        enforcer.enforce("connect")

        enforcer.set_mode(OperatingMode.ASTRONOMICAL)
        mount.start_tracking()  # the astronomical workflow turns tracking on

        assert enforcer.enforce("star_calibration").allowed
        assert mount.status().tracking is True

    def test_switching_astronomical_to_terrestrial_forces_tracking_off(self) -> None:
        mount = _tracking_mount()
        enforcer = _enforcer(mount, OperatingMode.ASTRONOMICAL)

        enforcer.set_mode(OperatingMode.TERRESTRIAL)

        assert mount.status().tracking is False

    def test_mode_can_be_set_while_disconnected_and_is_enforced_on_connect(self) -> None:
        mount = FakeMountPark(start_parked=False, available=False)
        enforcer = _enforcer(mount, OperatingMode.ASTRONOMICAL)
        enforcer.set_mode(OperatingMode.TERRESTRIAL)
        assert enforcer.mode is OperatingMode.TERRESTRIAL

        mount.make_available(tracking=True)  # mount connects with tracking ON
        gate = enforcer.enforce("connect")

        assert gate.allowed
        assert mount.status().tracking is False


class TestDiagnosticsTrail:
    def test_transitions_record_mode_before_command_and_after(self) -> None:
        mount = _tracking_mount()
        enforcer = _enforcer(mount, OperatingMode.TERRESTRIAL)

        enforcer.enforce("connect")

        evidence = enforcer.evidence()
        assert evidence["operating_mode"] == "terrestrial"
        step = evidence["transitions"][-1]
        assert step["context"] == "connect"
        assert step["tracking_before"] is True
        assert step["command"] == "stop_tracking"
        assert step["tracking_after"] is False
        assert step["allowed"] is True

    def test_a_blocked_measurement_is_recorded_with_its_reason(self) -> None:
        enforcer = _enforcer(_tracking_mount(refuse_stop=True), OperatingMode.TERRESTRIAL)

        enforcer.enforce("calibration")

        step = enforcer.evidence()["transitions"][-1]
        assert step["allowed"] is False
        assert step["reason"]

    def test_the_trail_is_bounded(self) -> None:
        enforcer = _enforcer(FakeMountPark(start_parked=False), OperatingMode.TERRESTRIAL)

        for _ in range(500):
            enforcer.enforce("poll")

        assert len(enforcer.evidence()["transitions"]) <= 100


class TestEnsureTrackingModeSettling:
    def test_a_mount_that_settles_late_is_confirmed_within_the_timeout(self) -> None:
        """The INDI driver applies the command asynchronously -- an immediate
        re-read can still show the old state."""

        class _Lagging(FakeMountPark):
            def __init__(self) -> None:
                super().__init__(start_parked=False)
                self._pending_off_after = 3

            def stop_tracking(self) -> None:
                self.stop_tracking_count += 1  # takes effect only after a few status polls

            def status(self) -> MountParkStatus:
                if self.stop_tracking_count and self._pending_off_after > 0:
                    self._pending_off_after -= 1
                    if self._pending_off_after == 0:
                        self._tracking = False
                return super().status()

        mount = _Lagging()
        mount.start_tracking()

        result = ensure_tracking_mode(
            mount, TrackingMode.OFF, settle_timeout_s=1.0, poll_interval_s=0.001
        )

        assert result.ok

    def test_default_behaviour_is_unchanged_without_a_settle_timeout(self) -> None:
        mount = _tracking_mount(refuse_stop=True)

        result = ensure_tracking_mode(mount, TrackingMode.OFF)

        assert not result.ok


# ---------------------------------------------------------------------------------------------
# S6.0c review C1/C2 (#44): while another operation holds the OnStep connection (e.g. a Mount
# Align GOTO), the park adapter serves a HELD-OVER reading (or "not known yet") so GUI polls
# never block. A DECISION must never treat that as verified: the gate fails CLOSED with an
# actionable reason, records no held-over value as the mount's state, sends no correction (it
# would queue behind that operation) -- and still never blocks the calling thread. "No mount"
# (an adapter that is not connected) keeps today's allow. Production park adapter on the #51
# simulator; `ObservableRLock` tells deterministically whether the decision got through
# ("done") or queued behind the held connection ("blocked").
# ---------------------------------------------------------------------------------------------


class _BusyConnection:
    """The real park adapter on a simulated OnStep whose connection another thread holds."""

    def __init__(self, *, read_first: bool) -> None:
        self.connection, made = make_simulated_onstep_connection(OnStepScenario(parked=False))
        self.lock = install_observable_operation_lock(self.connection)
        self.park = OnStepMountParkAdapter(self.connection)
        self.park.connect()
        self.client = made[0]
        self.client.tracking = False
        if read_first:
            assert self.park.status().fresh  # a GUI poll read tracking OFF ...
        self.client.tracking = True  # ... then the mount started tracking (e.g. OnStep's
        # delayed post-UNPARK auto-tracking) while another operation holds the connection
        self._release = threading.Event()
        held = threading.Event()

        def hold() -> None:
            with self.lock:
                held.set()
                self._release.wait(_JOIN_S)

        self._holder = threading.Thread(target=hold, daemon=True)
        self._holder.start()
        assert held.wait(_JOIN_S)

    def decide(self, decision: Callable[[], object]) -> tuple[str, list[object]]:
        """Runs `decision` on another thread while the connection is held: "done" (never
        queued) or "blocked" (queued behind the held operation), and what it returned."""
        got: list[object] = []
        worker = threading.Thread(target=lambda: got.append(decision()), daemon=True)
        worker.start()
        outcome = self.lock.wait_until_blocked_or_done(worker)
        self.release()
        worker.join(_JOIN_S)
        return outcome, got

    def release(self) -> None:
        self._release.set()
        self._holder.join(_JOIN_S)


class TestDecisionsNeverUseHeldOverReadings:
    @pytest.mark.parametrize("read_first", [True, False], ids=["held_over", "nothing_read_yet"])
    def test_terrestrial_gate_fails_closed_while_the_mount_is_busy(self, read_first: bool) -> None:
        busy = _BusyConnection(read_first=read_first)
        enforcer = TrackingEnforcer(busy.park, OperatingMode.TERRESTRIAL, settle_timeout_s=0)

        outcome, got = busy.decide(lambda: enforcer.enforce("autofocus"))

        assert outcome == "done", "the gate queued behind the held connection"
        (gate,) = got
        assert gate == TrackingGate(False, MOUNT_BUSY_REASON)
        assert not enforcer.measurement_allowed()
        step = enforcer.evidence()["transitions"][-1]
        assert step["allowed"] is False and step["reason"] == MOUNT_BUSY_REASON
        assert "verified (" not in step["reason"]  # never "tracking off verified (...)"
        assert step["tracking_before"] is None and step["tracking_after"] is None
        assert busy.client.emergency_stop_calls == 0  # no correction queued behind it

    def test_entering_terrestrial_mode_while_busy_is_denied_not_verified(self) -> None:
        busy = _BusyConnection(read_first=True)
        enforcer = TrackingEnforcer(busy.park, OperatingMode.ASTRONOMICAL, settle_timeout_s=0)

        outcome, got = busy.decide(lambda: enforcer.set_mode(OperatingMode.TERRESTRIAL))

        assert outcome == "done"
        assert got == [TrackingGate(False, MOUNT_BUSY_REASON)]

    @pytest.mark.parametrize("read_first", [True, False], ids=["held_over", "nothing_read_yet"])
    def test_ensure_tracking_mode_reports_busy_and_sends_nothing(self, read_first: bool) -> None:
        busy = _BusyConnection(read_first=read_first)

        outcome, got = busy.decide(lambda: ensure_tracking_mode(busy.park, TrackingMode.OFF))

        assert outcome == "done"
        assert got == [TrackingVerificationResult(TrackingVerificationStatus.BUSY, None)]
        assert busy.client.emergency_stop_calls == 0

    def test_once_the_connection_is_free_the_gate_verifies_and_repairs_again(self) -> None:
        busy = _BusyConnection(read_first=True)
        busy.release()
        enforcer = TrackingEnforcer(busy.park, OperatingMode.TERRESTRIAL, settle_timeout_s=0)

        gate = enforcer.enforce("autofocus")

        assert gate.allowed and gate.reason == "tracking off verified (repaired)"
        assert busy.client.tracking is False and busy.client.emergency_stop_calls == 1

    def test_no_mount_configured_keeps_todays_allow(self) -> None:
        connection, _made = make_simulated_onstep_connection(OnStepScenario(parked=False))
        park = OnStepMountParkAdapter(connection)  # never connected: there is no mount
        enforcer = TrackingEnforcer(park, OperatingMode.TERRESTRIAL, settle_timeout_s=0)

        gate = enforcer.enforce("autofocus")

        assert gate == TrackingGate(True, "mount unavailable: no tracking to enforce")

    def test_off_the_gui_thread_a_decision_waits_briefly_for_a_fresh_reading(self) -> None:
        """Re-review R1: a momentary hold (e.g. another thread's status poll) released within
        the decision's wait yields a fresh, real verification -- here a repair."""
        busy = _BusyConnection(read_first=True)

        outcome, got = busy.decide(
            lambda: ensure_tracking_mode(busy.park, TrackingMode.OFF, fresh_wait_s=5.0)
        )

        assert outcome == "blocked"  # it waited for the holder, which then let go
        assert got == [
            TrackingVerificationResult(TrackingVerificationStatus.REPAIRED, TrackingMode.OFF)
        ]

    def test_a_hold_longer_than_the_wait_is_still_busy(self) -> None:
        busy = _BusyConnection(read_first=True)
        try:
            result = ensure_tracking_mode(busy.park, TrackingMode.OFF, fresh_wait_s=0.05)
        finally:
            busy.release()
        assert result == TrackingVerificationResult(TrackingVerificationStatus.BUSY, None)
        assert busy.client.emergency_stop_calls == 0
