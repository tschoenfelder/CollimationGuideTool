"""S6.0b: the one rule for planning a solved move on an angular-only mount, shared by Mount
Align's screen moves and the guide-assisted recenter policy."""

from __future__ import annotations

import pytest
from astrotool_core.mount import AxisDirection, AxisResponse, CalibrationMatrix, MountAxis
from collimation_tool.application.angular_move_plan import (
    SCREEN_MOVE_DROP_TOLERANCE,
    plan_angular_move,
)
from collimation_tool.application.equivalent_duration import arcsec_to_equivalent_ms

_RATE = 8.0  # x sidereal: 1000 equivalent ms = 120.3"


def _matrix(px_per_ms: float = 0.01) -> CalibrationMatrix:
    """+AXIS1 -> +x, +AXIS2 -> +y at `px_per_ms` per equivalent ms."""
    responses = {}
    for axis in MountAxis:
        for direction in AxisDirection:
            delta = (1.0 if direction is AxisDirection.POSITIVE else -1.0) * 1000 * px_per_ms
            dx, dy = (delta, 0.0) if axis is MountAxis.AXIS1 else (0.0, delta)
            responses[(axis, direction)] = AxisResponse(
                axis=axis,
                direction=direction,
                duration_ms=1000,
                dx_px=dx,
                dy_px=dy,
                px_per_ms=abs(delta) / 1000,
            )
    return CalibrationMatrix(responses=responses)


def _ms(arcsec: float) -> int:
    return round(arcsec_to_equivalent_ms(arcsec, center_rate_x=_RATE))


def test_components_above_the_floor_become_angular_steps() -> None:
    solved = [
        (MountAxis.AXIS1, AxisDirection.POSITIVE, _ms(100.0)),
        (MountAxis.AXIS2, AxisDirection.NEGATIVE, _ms(50.0)),
    ]
    plan = plan_angular_move(
        _matrix(),
        solved,
        target_px=10.0,
        center_rate_x=_RATE,
        cap_arcsec=1000.0,
        floor_arcsec=30.0,
    )
    assert not plan.refused and not plan.clamped and plan.dropped == ()
    assert [(s.axis, s.direction) for s in plan.steps] == [
        (MountAxis.AXIS1, AxisDirection.POSITIVE),
        (MountAxis.AXIS2, AxisDirection.NEGATIVE),
    ]
    assert plan.steps[0].arcsec == pytest.approx(100.0, abs=0.1)
    assert plan.steps[1].duration_ms == _ms(50.0)


def test_a_component_above_the_cap_is_clamped() -> None:
    solved = [(MountAxis.AXIS1, AxisDirection.POSITIVE, _ms(500.0))]
    plan = plan_angular_move(
        _matrix(),
        solved,
        target_px=50.0,
        center_rate_x=_RATE,
        cap_arcsec=200.0,
        floor_arcsec=30.0,
    )
    assert plan.clamped and plan.steps[0].arcsec == 200.0
    assert plan.steps[0].duration_ms == _ms(200.0)


def test_a_small_sub_floor_component_is_dropped() -> None:
    # 1000 ms-eq = 10 px on AXIS1; 150 ms-eq (18") = 1.5 px on AXIS2: 1.5 / 10.1 < 25%.
    solved = [
        (MountAxis.AXIS1, AxisDirection.POSITIVE, 1000),
        (MountAxis.AXIS2, AxisDirection.POSITIVE, 150),
    ]
    plan = plan_angular_move(
        _matrix(),
        solved,
        target_px=10.1,
        center_rate_x=_RATE,
        cap_arcsec=1000.0,
        floor_arcsec=30.0,
    )
    assert not plan.refused
    assert [s.axis for s in plan.steps] == [MountAxis.AXIS1]
    assert [axis for axis, _arcsec in plan.dropped] == [MountAxis.AXIS2]


def test_a_large_sub_floor_component_refuses_the_whole_move() -> None:
    # AXIS2's 150 ms-eq leaves 1.5 of 3.4 px -- beyond 25%: nothing is planned at all.
    solved = [
        (MountAxis.AXIS1, AxisDirection.POSITIVE, 300),
        (MountAxis.AXIS2, AxisDirection.POSITIVE, 150),
    ]
    plan = plan_angular_move(
        _matrix(),
        solved,
        target_px=3.4,
        center_rate_x=_RATE,
        cap_arcsec=1000.0,
        floor_arcsec=30.0,
    )
    assert plan.refused and plan.steps == ()
    # ... unless the caller's tolerance allows that error (the recenter loop's 50%).
    looser = plan_angular_move(
        _matrix(),
        solved,
        target_px=3.4,
        center_rate_x=_RATE,
        cap_arcsec=1000.0,
        floor_arcsec=30.0,
        drop_tolerance=0.5,
    )
    assert not looser.refused and [s.axis for s in looser.steps] == [MountAxis.AXIS1]


def test_only_sub_floor_components_refuse() -> None:
    solved = [(MountAxis.AXIS1, AxisDirection.POSITIVE, 100)]
    plan = plan_angular_move(
        _matrix(),
        solved,
        target_px=1.0,
        center_rate_x=_RATE,
        cap_arcsec=1000.0,
        floor_arcsec=30.0,
    )
    assert plan.refused and plan.steps == ()


def test_the_screen_move_tolerance_is_a_quarter() -> None:
    assert SCREEN_MOVE_DROP_TOLERANCE == 0.25


def test_per_axis_caps_clamp_each_component_on_its_own() -> None:
    """Re-review of S6.0b: a cap must never push a component that reaches the floor below it
    -- each component is clamped on its own, and the drop rule sees the full shift."""
    solved = [
        (MountAxis.AXIS1, AxisDirection.POSITIVE, _ms(600.0)),
        (MountAxis.AXIS2, AxisDirection.POSITIVE, _ms(40.0)),
    ]
    plan = plan_angular_move(
        _matrix(),
        solved,
        target_px=50.0,
        center_rate_x=_RATE,
        cap_arcsec=1000.0,
        floor_arcsec=30.0,
        axis_cap_arcsec={MountAxis.AXIS1: 60.0, MountAxis.AXIS2: 120.0},
    )
    assert plan.clamped and not plan.refused and plan.dropped == ()
    assert [(s.axis, round(s.arcsec)) for s in plan.steps] == [
        (MountAxis.AXIS1, 60),
        (MountAxis.AXIS2, 40),
    ]
