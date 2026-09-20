"""Issue #44: one explicit, mode-aware tracking policy.

TERRESTRIAL => mount tracking required OFF (fail closed if it cannot be
turned off); ASTRONOMICAL => the policy never forces tracking."""

from __future__ import annotations

from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer
from astrotool_core.mount.park_port import MountParkStatus
from astrotool_core.mount.tracking_mode import TrackingMode, ensure_tracking_mode
from astrotool_core.testing.fake_mount_park import FakeMountPark


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
