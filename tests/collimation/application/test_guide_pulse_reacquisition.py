"""S6.0d (#39): which motion a reacquisition may use, and the guide-pulse calibration --
low-tier tests with plain fakes (the end-to-end behaviour on the production adapter + simulator
is in test_star_acquisition_guide_pulse_simulated.py)."""

from __future__ import annotations

import math

import pytest
from astrotool_core.mount import (
    AxisDirection,
    CommandResult,
    GuidePulseResult,
    MountAxis,
    MountCapabilities,
    MountParkStatus,
    MountStatus,
)
from astrotool_core.mount.operating_mode import OperatingMode
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState
from astrotool_core.testing import FakeMountPark
from collimation_tool.application.guide_pulse_reacquisition import (
    ASTRONOMICAL_NEEDS_GUIDE_PULSES,
    MOUNT_NOT_CONNECTED,
    TRACKING_STATE_UNKNOWN,
    GuidePulseCalibrationConfig,
    ReacquisitionMotion,
    calibrate_guide_pulses,
    choose_reacquisition_motion,
    read_tracking_for_decision,
)

_SIGN = {AxisDirection.POSITIVE: 1.0, AxisDirection.NEGATIVE: -1.0}


class _Mount:
    """A guide-pulse mount whose star moves `px_per_ms[(axis, direction)]` per guide ms."""

    def __init__(
        self,
        *,
        capable: bool = True,
        connected: bool = True,
        vectors: dict[tuple[MountAxis, AxisDirection], tuple[float, float]] | None = None,
        refuse: GuidePulseResult | None = None,
    ) -> None:
        self.capable = capable
        self.connected = connected
        self.position = (100.0, 100.0)
        self.vectors = vectors or {
            (axis, direction): (
                (_SIGN[direction] * 0.01, 0.0)
                if axis is MountAxis.AXIS1
                else (0.0, _SIGN[direction] * 0.01)
            )
            for axis in MountAxis
            for direction in AxisDirection
        }
        self.refuse = refuse
        self.pulses: list[tuple[MountAxis, AxisDirection, int]] = []

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        raise AssertionError("guide-pulse reacquisition never uses pulse_axis")

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(False, 0, 0, supports_guide_pulses_while_tracking=self.capable)

    def status(self) -> MountStatus:
        return MountStatus(connected=self.connected, tracking=True, slewing=False)

    @property
    def guide_pulse_range_ms(self) -> tuple[int, int] | None:
        return (20, 5000) if self.capable else None

    def guide_pulse(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int
    ) -> GuidePulseResult:
        self.pulses.append((axis, direction, duration_ms))
        if self.refuse is not None:
            return self.refuse
        vx, vy = self.vectors[(axis, direction)]
        self.position = (self.position[0] + vx * duration_ms, self.position[1] + vy * duration_ms)
        return GuidePulseResult(accepted=True, sent=True, tracking_preserved=True)

    def measure(self) -> TrackingResult:
        return TrackingResult(
            state=TrackingState.LOCKED,
            x=self.position[0],
            y=self.position[1],
            matched_source=None,
        )


class TestChooseReacquisitionMotion:
    @pytest.mark.parametrize("mode", [None, OperatingMode.TERRESTRIAL])
    def test_terrestrial_keeps_the_angular_path_without_reading_tracking(
        self, mode: OperatingMode | None
    ) -> None:
        def tracking() -> bool | None:
            raise AssertionError("terrestrial must not read tracking here")

        decision = choose_reacquisition_motion(mode, tracking, _Mount())

        assert decision.motion is ReacquisitionMotion.ANGULAR and decision.refusal is None

    def test_astronomical_tracking_off_uses_the_angular_path(self) -> None:
        decision = choose_reacquisition_motion(OperatingMode.ASTRONOMICAL, lambda: False, _Mount())

        assert decision.motion is ReacquisitionMotion.ANGULAR

    def test_astronomical_tracking_on_uses_guide_pulses_when_capable(self) -> None:
        decision = choose_reacquisition_motion(OperatingMode.ASTRONOMICAL, lambda: True, _Mount())

        assert decision.motion is ReacquisitionMotion.GUIDE_PULSE

    def test_astronomical_tracking_on_without_the_capability_is_refused(self) -> None:
        decision = choose_reacquisition_motion(
            OperatingMode.ASTRONOMICAL, lambda: True, _Mount(capable=False)
        )

        assert decision.motion is None
        assert decision.refusal == ASTRONOMICAL_NEEDS_GUIDE_PULSES
        assert decision.refusal == (
            "astronomical reacquisition needs OnStepAdapter ≥ 0.5.0 (guide pulses while tracking)"
        )

    def test_a_disconnected_mount_is_not_blamed_on_the_onstepadapter_version(self) -> None:
        decision = choose_reacquisition_motion(
            OperatingMode.ASTRONOMICAL, lambda: True, _Mount(capable=False, connected=False)
        )

        assert decision.refusal == MOUNT_NOT_CONNECTED

    @pytest.mark.parametrize("tracking", [None, "missing"])
    def test_an_unknown_tracking_state_fails_closed(self, tracking: str | None) -> None:
        reader = None if tracking == "missing" else (lambda: None)

        decision = choose_reacquisition_motion(OperatingMode.ASTRONOMICAL, reader, _Mount())

        assert decision.motion is None and decision.refusal == TRACKING_STATE_UNKNOWN


class TestReadTrackingForDecision:
    def test_a_fresh_available_reading_is_used(self) -> None:
        park = FakeMountPark(start_parked=False)
        park.start_tracking()

        assert read_tracking_for_decision(park) is True

    @pytest.mark.parametrize(
        "status",
        [
            MountParkStatus(available=True, parked=False, tracking=True, fresh=False),
            MountParkStatus(available=False, parked=False, tracking=False),
        ],
    )
    def test_a_held_over_or_missing_reading_is_unknown(self, status: MountParkStatus) -> None:
        class _Park(FakeMountPark):
            def decision_status(self, *, wait_fresh_s: float) -> MountParkStatus:
                assert wait_fresh_s > 0  # a decision off the GUI thread waits for a fresh read
                return status

        assert read_tracking_for_decision(_Park()) is None


class TestCalibrateGuidePulses:
    def test_each_direction_gets_its_own_measured_response(self) -> None:
        vectors = {
            (MountAxis.AXIS1, AxisDirection.POSITIVE): (0.006, 0.002),
            (MountAxis.AXIS1, AxisDirection.NEGATIVE): (-0.004, -0.001),  # asymmetric RA
            (MountAxis.AXIS2, AxisDirection.POSITIVE): (-0.001, 0.005),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE): (0.001, -0.005),
        }
        mount = _Mount(vectors=vectors)

        outcome = calibrate_guide_pulses(mount, mount.measure)

        assert outcome.failure_reason is None and outcome.matrix is not None
        for key, (vx, vy) in vectors.items():
            response = outcome.matrix.response_for(*key)
            assert response.dx_px / response.duration_ms == pytest.approx(vx)
            assert response.dy_px / response.duration_ms == pytest.approx(vy)
        # Paired directions: the star ends where it started (within the asymmetry).
        assert [p[:2] for p in mount.pulses][:1] == [(MountAxis.AXIS1, AxisDirection.POSITIVE)]

    def test_probes_escalate_only_until_the_star_moved_enough(self) -> None:
        # 0.0015 px/ms: cumulative 250/1000/2000 ms -> 0.4/1.5/3.0 px (< 4), 5000 ms -> 7.5 px.
        slow = {
            (axis, direction): (
                (_SIGN[direction] * 0.0015, 0.0)
                if axis is MountAxis.AXIS1
                else (0.0, _SIGN[direction] * 0.0015)
            )
            for axis in MountAxis
            for direction in AxisDirection
        }
        mount = _Mount(vectors=slow)

        outcome = calibrate_guide_pulses(mount, mount.measure)

        assert outcome.matrix is not None
        assert [p[2] for p in mount.pulses] == [250, 750, 1000, 3000] * 4
        first = outcome.matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
        assert first.duration_ms == 5000
        assert sum(p[2] for p in mount.pulses) <= 4 * 5000  # bounded

    def test_too_little_motion_is_an_explicit_failure(self) -> None:
        tiny = {
            (axis, direction): (_SIGN[direction] * 0.0001, 0.0001)
            for axis in MountAxis
            for direction in AxisDirection
        }
        mount = _Mount(vectors=tiny)

        outcome = calibrate_guide_pulses(mount, mount.measure)

        assert outcome.matrix is None
        assert outcome.failure_reason == "guide_pulse_calibration_failed"
        assert "5000 ms" in outcome.message and "too low" in outcome.message
        assert [p[2] for p in mount.pulses] == [250, 750, 1000, 3000]  # never beyond 5 s

    def test_parallel_responses_are_refused(self) -> None:
        parallel = {
            (axis, direction): (_SIGN[direction] * 0.01, _SIGN[direction] * 0.01 * (1 + 1e-6))
            for axis in MountAxis
            for direction in AxisDirection
        }
        mount = _Mount(vectors=parallel)

        outcome = calibrate_guide_pulses(mount, mount.measure)

        assert outcome.failure_reason == "guide_pulse_calibration_failed"
        assert "parallel" in outcome.message

    @pytest.mark.parametrize(
        ("refusal", "reason"),
        [
            (
                GuidePulseResult(False, "guide pulse refused at meridian phase hard_stop"),
                "mount_correction_rejected",
            ),
            (
                GuidePulseResult(False, "x -- NOT tracking", sent=True, tracking_off=True),
                "mount_tracking_stopped",
            ),
        ],
    )
    def test_a_refused_pulse_ends_the_calibration_with_its_message(
        self, refusal: GuidePulseResult, reason: str
    ) -> None:
        mount = _Mount(refuse=refusal)

        outcome = calibrate_guide_pulses(mount, mount.measure)

        assert outcome.failure_reason == reason
        assert outcome.message == refusal.message
        assert len(mount.pulses) == 1

    def test_a_lost_star_and_a_cancel_are_reported(self) -> None:
        mount = _Mount()

        lost = calibrate_guide_pulses(
            mount, lambda: TrackingResult(TrackingState.LOST, None, None, None)
        )
        cancelled = calibrate_guide_pulses(mount, mount.measure, cancel_check=lambda: True)

        assert lost.failure_reason == "target_not_found_guide"
        assert cancelled.failure_reason == "cancelled"

    def test_probes_outside_the_mounts_range_are_refused_unsent(self) -> None:
        mount = _Mount()

        outcome = calibrate_guide_pulses(
            mount, mount.measure, config=GuidePulseCalibrationConfig(probe_ms=(6000,))
        )

        assert outcome.failure_reason == "guide_pulse_calibration_failed"
        assert mount.pulses == []

    def test_the_response_magnitude_is_per_ms(self) -> None:
        mount = _Mount()

        outcome = calibrate_guide_pulses(mount, mount.measure)

        assert outcome.matrix is not None
        response = outcome.matrix.response_for(MountAxis.AXIS2, AxisDirection.NEGATIVE)
        magnitude = math.hypot(response.dx_px, response.dy_px)
        assert response.px_per_ms == pytest.approx(magnitude / 1000)


class TestCalibrationProbesStayInsideTheTracker:
    """Review fix 4 (A1): a fast guide rate at a fine plate scale must not jump the star out of
    the guide tracker -- the first probe is short, and later probes are capped from the measured
    response so the cumulative excursion stays within `max_displacement_px`."""

    @staticmethod
    def _rate(px_per_ms: float) -> dict[tuple[MountAxis, AxisDirection], tuple[float, float]]:
        return {
            (axis, direction): (
                (_SIGN[direction] * px_per_ms, 0.0)
                if axis is MountAxis.AXIS1
                else (0.0, _SIGN[direction] * px_per_ms)
            )
            for axis in MountAxis
            for direction in AxisDirection
        }

    def test_the_first_probe_is_short(self) -> None:
        # 0.05 px/ms (e.g. 1x sidereal at 0.3"/px): a 1000 ms first probe would move 50 px.
        mount = _Mount(vectors=self._rate(0.05))

        outcome = calibrate_guide_pulses(mount, mount.measure, max_displacement_px=28.0)

        assert outcome.matrix is not None
        assert mount.pulses[0][2] <= 500
        assert max(math.hypot(*mount.vectors[p[:2]]) * p[2] for p in mount.pulses) <= 28.0

    def test_escalation_is_capped_by_the_measured_response(self) -> None:
        mount = _Mount(vectors=self._rate(0.0038))
        start = mount.position

        outcome = calibrate_guide_pulses(mount, mount.measure, max_displacement_px=5.0)

        assert outcome.matrix is not None
        west = [p[2] for p in mount.pulses if p[:2] == (MountAxis.AXIS1, AxisDirection.POSITIVE)]
        assert sum(west) * 0.0038 <= 5.0 + 1e-9  # never beyond the allowed excursion
        assert sum(west) * 0.0038 >= 4.0  # and still enough to calibrate
        assert sum(west) <= 5000
        assert mount.position == pytest.approx(start, abs=1e-6)  # paired directions
