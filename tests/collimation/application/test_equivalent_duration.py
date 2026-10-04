"""S6.0b: the single owner of the angular-only ms<->arcsec factor."""

from __future__ import annotations

import math

import pytest
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount.movement_sizing import (
    SIDEREAL_ARCSEC_PER_S,
    CameraGeometry,
    SizingPolicy,
    plan_first_move,
)
from collimation_tool.application.equivalent_duration import (
    angular_unit_rate_arcsec_per_s,
    arcsec_to_equivalent_ms,
    equivalent_ms_to_arcsec,
)


def test_the_unit_rate_is_center_rate_times_sidereal() -> None:
    assert angular_unit_rate_arcsec_per_s(8.0) == pytest.approx(8.0 * SIDEREAL_ARCSEC_PER_S)
    assert angular_unit_rate_arcsec_per_s(0.5) == pytest.approx(0.5 * SIDEREAL_ARCSEC_PER_S)


def test_the_default_configuration_is_eight_times_sidereal() -> None:
    center = MountAlignmentSettings().calibration_center_rate_x
    assert equivalent_ms_to_arcsec(1000, center_rate_x=center) == pytest.approx(120.328)


def test_conversions_round_trip() -> None:
    for center in (0.25, 8.0, 20.0):
        for arcsec in (30.0, 123.4, 36000.0):
            ms = arcsec_to_equivalent_ms(arcsec, center_rate_x=center)
            assert equivalent_ms_to_arcsec(ms, center_rate_x=center) == pytest.approx(arcsec)


def test_a_first_move_sized_by_plan_first_move_maps_back_to_its_calculated_angle() -> None:
    """The matrix unit and Mount Align's seed sizing share one factor: an unclamped seed
    duration converts back to exactly the planned target angle."""
    camera = CameraGeometry("left", 1000, 600, 0.5)  # 500" wide -> 125" target
    policy = SizingPolicy(center_rate_x=8.0, min_duration_ms=1, max_duration_ms=100_000)
    plan = plan_first_move([camera], policy)
    assert plan.target_arcsec is not None
    assert equivalent_ms_to_arcsec(plan.duration_ms, center_rate_x=8.0) == pytest.approx(
        plan.target_arcsec, abs=equivalent_ms_to_arcsec(0.5, center_rate_x=8.0)
    )


@pytest.mark.parametrize("bad", [0.0, -1.0, math.inf, math.nan])
def test_an_invalid_center_rate_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="center rate"):
        angular_unit_rate_arcsec_per_s(bad)
