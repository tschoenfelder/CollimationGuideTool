import pytest
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import AxisDirection, MountAxis
from guide_tool.domain.correction_model import GuideCorrectionConfig, compute_would_pulses

_RESPONSE_VECTORS = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): (10.0, 0.0),
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): (-10.0, 0.0),
    (MountAxis.AXIS2, AxisDirection.POSITIVE): (0.0, 10.0),
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): (0.0, -10.0),
}


def make_calibration(px_per_ms: float = 0.1) -> CalibrationMatrix:
    responses = {
        (axis, direction): AxisResponse(
            axis=axis, direction=direction, duration_ms=100, dx_px=dx, dy_px=dy, px_per_ms=px_per_ms
        )
        for (axis, direction), (dx, dy) in _RESPONSE_VECTORS.items()
    }
    return CalibrationMatrix(responses=responses)


def test_error_within_deadband_produces_no_pulses() -> None:
    calibration = make_calibration()
    pulses = compute_would_pulses(0.2, 0.1, calibration, GuideCorrectionConfig())
    assert pulses == []


def test_error_too_small_for_min_pulse_ms_is_skipped() -> None:
    calibration = make_calibration()
    # 5px * 0.7 aggressiveness / 0.1 px/ms = 35ms, below the 50ms default floor
    pulses = compute_would_pulses(5.0, 0.0, calibration, GuideCorrectionConfig())
    assert pulses == []


def test_positive_error_picks_the_opposing_negative_direction() -> None:
    calibration = make_calibration()
    pulses = compute_would_pulses(10.0, 0.0, calibration, GuideCorrectionConfig())
    assert len(pulses) == 1
    assert pulses[0].axis is MountAxis.AXIS1
    assert pulses[0].direction is AxisDirection.NEGATIVE
    assert pulses[0].duration_ms == pytest.approx(70, abs=1)


def test_negative_error_picks_the_opposing_positive_direction() -> None:
    calibration = make_calibration()
    pulses = compute_would_pulses(-10.0, 0.0, calibration, GuideCorrectionConfig())
    assert pulses[0].direction is AxisDirection.POSITIVE


def test_both_axes_can_pulse_independently() -> None:
    calibration = make_calibration()
    pulses = compute_would_pulses(20.0, -20.0, calibration, GuideCorrectionConfig())
    axes = {pulse.axis for pulse in pulses}
    assert axes == {MountAxis.AXIS1, MountAxis.AXIS2}


def test_axis2_disabled_only_corrects_axis1() -> None:
    calibration = make_calibration()
    config = GuideCorrectionConfig(axis2_enabled=False)
    pulses = compute_would_pulses(20.0, -20.0, calibration, config)
    assert {pulse.axis for pulse in pulses} == {MountAxis.AXIS1}


def test_duration_is_clamped_to_max_pulse_ms() -> None:
    calibration = make_calibration()
    config = GuideCorrectionConfig(max_pulse_ms=100)
    pulses = compute_would_pulses(1000.0, 0.0, calibration, config)
    assert pulses[0].duration_ms == 100
