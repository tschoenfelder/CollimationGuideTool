import math

import pytest
from astrotool_core.mount.axis_calibration import (
    AxisResponse,
    calibrate_axes,
    calibrate_axis,
    calibrate_axis_multi,
    compose_screen_move,
)
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.testing.fake_mount import FakeMountAdapter


def test_calibrate_axis_computes_displacement_and_rate() -> None:
    mount = FakeMountAdapter()
    mount.connect()
    positions = iter([(100.0, 100.0), (120.0, 100.0)])

    response = calibrate_axis(
        mount,
        MountAxis.AXIS1,
        AxisDirection.POSITIVE,
        measure=lambda: next(positions),
        pulse_ms=500,
    )

    assert response.axis is MountAxis.AXIS1
    assert response.direction is AxisDirection.POSITIVE
    assert response.duration_ms == 500
    assert response.dx_px == pytest.approx(20.0)
    assert response.dy_px == pytest.approx(0.0)
    assert response.px_per_ms == pytest.approx(0.04)


def test_calibrate_axis_sends_the_pulse_to_the_mount() -> None:
    mount = FakeMountAdapter()
    mount.connect()
    positions = iter([(0.0, 0.0), (0.0, 0.0)])

    calibrate_axis(
        mount,
        MountAxis.AXIS2,
        AxisDirection.NEGATIVE,
        measure=lambda: next(positions),
        pulse_ms=250,
    )

    assert mount.pulse_log == [(MountAxis.AXIS2, AxisDirection.NEGATIVE, 250)]


def test_calibrate_axis_raises_when_pulse_rejected() -> None:
    mount = FakeMountAdapter()  # never connected -> pulse_axis always rejects
    with pytest.raises(RuntimeError, match="pulse rejected"):
        calibrate_axis(
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            measure=lambda: (0.0, 0.0),
            pulse_ms=500,
        )


def test_zero_duration_pulse_has_zero_rate_instead_of_dividing_by_zero() -> None:
    mount = FakeMountAdapter()
    mount.connect()
    positions = iter([(0.0, 0.0), (5.0, 0.0)])

    response = calibrate_axis(
        mount,
        MountAxis.AXIS1,
        AxisDirection.POSITIVE,
        measure=lambda: next(positions),
        pulse_ms=0,
    )
    assert response.px_per_ms == 0.0


def test_calibrate_axes_covers_every_axis_direction_combination() -> None:
    mount = FakeMountAdapter()
    mount.connect()
    # Each call returns a distinct (before, after) pair advancing by (10, 0) px.
    call_count = 0

    def measure() -> tuple[float, float]:
        nonlocal call_count
        call_count += 1
        return (float(call_count) * 10.0, 0.0)

    matrix = calibrate_axes(
        mount,
        measure=measure,
        pulse_ms=100,
        axes=(MountAxis.AXIS1, MountAxis.AXIS2),
        directions=(AxisDirection.POSITIVE, AxisDirection.NEGATIVE),
    )

    assert set(matrix.responses.keys()) == {
        (MountAxis.AXIS1, AxisDirection.POSITIVE),
        (MountAxis.AXIS1, AxisDirection.NEGATIVE),
        (MountAxis.AXIS2, AxisDirection.POSITIVE),
        (MountAxis.AXIS2, AxisDirection.NEGATIVE),
    }
    for response in matrix.responses.values():
        assert response.dx_px == pytest.approx(10.0)


def test_axis_response_magnitude_and_angle() -> None:
    response = AxisResponse(
        axis=MountAxis.AXIS1,
        direction=AxisDirection.POSITIVE,
        duration_ms=500,
        dx_px=3.0,
        dy_px=4.0,
        px_per_ms=0.01,
    )
    assert response.magnitude_px == pytest.approx(5.0)
    assert response.angle_degrees == pytest.approx(math.degrees(math.atan2(4.0, 3.0)))


def test_axis_response_angle_is_zero_for_no_motion() -> None:
    response = AxisResponse(
        axis=MountAxis.AXIS1,
        direction=AxisDirection.POSITIVE,
        duration_ms=500,
        dx_px=0.0,
        dy_px=0.0,
        px_per_ms=0.0,
    )
    assert response.angle_degrees == 0.0


def test_axis_response_angle_is_normalized_to_0_360() -> None:
    # dx negative, dy negative -> third quadrant, atan2 alone would be
    # negative; angle_degrees must normalize into [0, 360).
    response = AxisResponse(
        axis=MountAxis.AXIS1,
        direction=AxisDirection.POSITIVE,
        duration_ms=500,
        dx_px=-1.0,
        dy_px=-1.0,
        px_per_ms=0.0,
    )
    assert 180.0 < response.angle_degrees < 270.0


def test_calibrate_axis_multi_pulses_once_and_measures_every_measurer() -> None:
    mount = FakeMountAdapter()
    mount.connect()
    left_positions = iter([(0.0, 0.0), (10.0, 0.0)])
    right_positions = iter([(5.0, 5.0), (5.0, 15.0)])

    responses = calibrate_axis_multi(
        mount,
        MountAxis.AXIS2,
        AxisDirection.POSITIVE,
        measures={
            "left": lambda: next(left_positions),
            "right": lambda: next(right_positions),
        },
        pulse_ms=500,
    )

    # Exactly one pulse for the whole call, not one per measurer.
    assert mount.pulse_log == [(MountAxis.AXIS2, AxisDirection.POSITIVE, 500)]
    assert set(responses) == {"left", "right"}
    assert responses["left"].dx_px == pytest.approx(10.0)
    assert responses["left"].dy_px == pytest.approx(0.0)
    assert responses["right"].dx_px == pytest.approx(0.0)
    assert responses["right"].dy_px == pytest.approx(10.0)


def test_calibrate_axis_multi_raises_when_pulse_rejected() -> None:
    mount = FakeMountAdapter()  # never connected -> pulse_axis always rejects
    with pytest.raises(RuntimeError, match="pulse rejected"):
        calibrate_axis_multi(
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            measures={"left": lambda: (0.0, 0.0)},
            pulse_ms=500,
        )


def _response(axis: MountAxis, dx_px: float, dy_px: float, duration_ms: int = 1000) -> AxisResponse:
    return AxisResponse(
        axis=axis,
        direction=AxisDirection.POSITIVE,
        duration_ms=duration_ms,
        dx_px=dx_px,
        dy_px=dy_px,
        px_per_ms=math.hypot(dx_px, dy_px) / duration_ms if duration_ms > 0 else 0.0,
    )


def test_compose_screen_move_axis_aligned_camera_uses_only_the_matching_axis() -> None:
    # AXIS1+ moves purely +x (right), AXIS2+ moves purely +y (down) at
    # 0.1 px/ms each -- a camera with no rotation relative to the mount.
    axis1 = _response(MountAxis.AXIS1, dx_px=100.0, dy_px=0.0)
    axis2 = _response(MountAxis.AXIS2, dx_px=0.0, dy_px=100.0)

    right = compose_screen_move(axis1, axis2, target_dx_px=10.0, target_dy_px=0.0)
    assert right == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 100)]

    up = compose_screen_move(axis1, axis2, target_dx_px=0.0, target_dy_px=-10.0)
    assert up == [(MountAxis.AXIS2, AxisDirection.NEGATIVE, 100)]


def test_compose_screen_move_rotated_camera_needs_both_axes() -> None:
    # AXIS1+ moves (+x, +y), AXIS2+ moves (+x, -y) -- a camera rotated 45
    # degrees relative to the mount, so neither axis alone is "right".
    axis1 = _response(MountAxis.AXIS1, dx_px=1000.0, dy_px=1000.0)
    axis2 = _response(MountAxis.AXIS2, dx_px=1000.0, dy_px=-1000.0)

    steps = compose_screen_move(axis1, axis2, target_dx_px=200.0, target_dy_px=0.0)

    assert steps == [
        (MountAxis.AXIS1, AxisDirection.POSITIVE, 100),
        (MountAxis.AXIS2, AxisDirection.POSITIVE, 100),
    ]


def test_compose_screen_move_omits_a_step_whose_duration_rounds_to_zero() -> None:
    axis1 = _response(MountAxis.AXIS1, dx_px=100.0, dy_px=0.0)
    axis2 = _response(MountAxis.AXIS2, dx_px=0.0, dy_px=100.0)

    left = compose_screen_move(axis1, axis2, target_dx_px=-10.0, target_dy_px=0.0)

    assert left == [(MountAxis.AXIS1, AxisDirection.NEGATIVE, 100)]
    assert all(axis is not MountAxis.AXIS2 for axis, _, _ in left)


def test_compose_screen_move_raises_for_near_parallel_axes() -> None:
    # AXIS1+ and AXIS2+ both move purely +x -- degenerate, can't span the
    # image plane, inverting would blow up.
    axis1 = _response(MountAxis.AXIS1, dx_px=100.0, dy_px=0.0)
    axis2 = _response(MountAxis.AXIS2, dx_px=200.0, dy_px=0.0)

    with pytest.raises(ValueError, match="parallel"):
        compose_screen_move(axis1, axis2, target_dx_px=10.0, target_dy_px=0.0)


def test_compose_screen_move_rejects_a_zero_duration_calibration_response() -> None:
    axis1 = _response(MountAxis.AXIS1, dx_px=0.0, dy_px=0.0, duration_ms=0)
    axis2 = _response(MountAxis.AXIS2, dx_px=0.0, dy_px=100.0)

    with pytest.raises(ValueError, match="duration_ms"):
        compose_screen_move(axis1, axis2, target_dx_px=10.0, target_dy_px=0.0)


def test_calibration_matrix_response_for_looks_up_by_axis_and_direction() -> None:
    mount = FakeMountAdapter()
    mount.connect()
    positions = iter([(0.0, 0.0), (30.0, 0.0)])
    matrix = calibrate_axes(
        mount,
        measure=lambda: next(positions),
        pulse_ms=100,
        axes=(MountAxis.AXIS1,),
        directions=(AxisDirection.POSITIVE,),
    )
    response = matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
    assert response.dx_px == pytest.approx(30.0)
