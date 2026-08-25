import pytest
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState
from collimation_tool.application.recenter_policy import CollimationRecenterPolicy, RecenterConfig

_RESPONSE_VECTORS = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): (10.0, 0.0),
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): (-10.0, 0.0),
    (MountAxis.AXIS2, AxisDirection.POSITIVE): (0.0, 10.0),
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): (0.0, -10.0),
}


def make_calibration(px_per_ms: float = 0.1, duration_ms: int = 100) -> CalibrationMatrix:
    responses = {
        (axis, direction): AxisResponse(
            axis=axis,
            direction=direction,
            duration_ms=duration_ms,
            dx_px=dx,
            dy_px=dy,
            px_per_ms=px_per_ms,
        )
        for (axis, direction), (dx, dy) in _RESPONSE_VECTORS.items()
    }
    return CalibrationMatrix(responses=responses)


class MovingMount:
    """Test double: applies each accepted pulse to a simulated star position."""

    def __init__(
        self, calibration: CalibrationMatrix, start: tuple[float, float], *, accept: bool = True
    ) -> None:
        self._calibration = calibration
        self.x, self.y = start
        self.pulse_log: list[tuple[MountAxis, AxisDirection, int]] = []
        self._accept = accept

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9999)

    def status(self) -> MountStatus:
        return MountStatus(connected=True, tracking=True, slewing=False)

    def pulse_axis(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int
    ) -> CommandResult:
        self.pulse_log.append((axis, direction, duration_ms))
        if not self._accept:
            return CommandResult(accepted=False, message="rejected for test")
        response = self._calibration.response_for(axis, direction)
        per_ms_x = response.dx_px / response.duration_ms
        per_ms_y = response.dy_px / response.duration_ms
        self.x += per_ms_x * duration_ms
        self.y += per_ms_y * duration_ms
        return CommandResult(accepted=True)


def _locked(mount: MovingMount) -> TrackingResult:
    return TrackingResult(state=TrackingState.LOCKED, x=mount.x, y=mount.y, matched_source=None)


def test_converges_on_the_dominant_axis_in_one_pulse() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is True
    assert result.reason == "within_tolerance"
    assert result.pulses_issued == 1
    assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.NEGATIVE, 500)]


def test_negative_offset_picks_the_opposing_positive_direction() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(-50.0, 0.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is True
    assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)]


def test_dominant_axis_chosen_by_larger_offset_component() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(10.0, 50.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert mount.pulse_log[0][0] is MountAxis.AXIS2


def test_pulse_duration_is_clamped_to_max_pulse_ms() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(10_000.0, 0.0))
    config = RecenterConfig(settle_ms=0, max_pulse_ms=200)
    policy = CollimationRecenterPolicy(mount, calibration, config)

    policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert mount.pulse_log[0][2] == 200


def test_already_within_tolerance_issues_no_pulses() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(1.0, 1.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is True
    assert result.pulses_issued == 0


def test_star_lost_aborts_immediately() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    lost = TrackingResult(state=TrackingState.LOST, x=50.0, y=0.0, matched_source=None)
    result = policy.center(lambda: lost, reference=(0.0, 0.0))

    assert result.success is False
    assert result.reason == "star_lost"
    assert result.pulses_issued == 0


def test_rejected_pulse_aborts_with_pulse_rejected_reason() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0), accept=False)
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is False
    assert result.reason == "pulse_rejected"
    assert result.pulses_issued == 1


def test_cancel_check_aborts_before_the_next_measurement() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0), cancel_check=lambda: True)

    assert result.success is False
    assert result.reason == "cancelled"
    assert result.pulses_issued == 0


def test_diverging_offset_aborts_after_max_diverge_count() -> None:
    calibration = make_calibration()
    # A mount that moves the star further away every pulse (broken calibration sign).
    positions = iter([50.0, 60.0, 75.0, 95.0])

    class DivergingMount(MovingMount):
        def pulse_axis(
            self, axis: MountAxis, direction: AxisDirection, duration_ms: int
        ) -> CommandResult:
            self.pulse_log.append((axis, direction, duration_ms))
            self.x = next(positions)
            return CommandResult(accepted=True)

    mount = DivergingMount(calibration, start=(50.0, 0.0))
    policy = CollimationRecenterPolicy(
        mount, calibration, RecenterConfig(settle_ms=0, max_diverge_count=2, max_iterations=10)
    )

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is False
    assert result.reason == "diverging"


def test_max_iterations_reports_final_offset_and_pass_fail_by_rough_tolerance() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0))
    # max_pulse_ms=10 clamps each correction to a 1.0px step (0.1 px/ms x 10ms) —
    # far too small to reach fine_tolerance_px within 2 iterations.
    policy = CollimationRecenterPolicy(
        mount,
        calibration,
        RecenterConfig(settle_ms=0, max_pulse_ms=10, max_iterations=2, rough_tolerance_px=50.0),
    )

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.reason == "max_pulses"
    assert result.pulses_issued == 2
    assert result.final_offset_px == pytest.approx(48.0)
    assert result.success is True  # within the generous rough tolerance
