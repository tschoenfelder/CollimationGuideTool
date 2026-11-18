"""Operator request (2026-11-18): on a mount with angular moves the RA/Dec buttons move FIXED
angles on the sky -- Small 1, Medium 5, Large 20 degrees -- whatever the camera optics or
calibration. A 20 degree step exceeds OnStepAdapter's 15 degree single-move bound, so the
production `OnStepMountPulseAdapter` runs it as equal parts."""

from __future__ import annotations

import math

import pytest
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, MountAxis
from collimation_tool.application.mount_align_plan import MovementSize
from collimation_tool.application.mount_align_positioning import nudge_angle_arcsec
from test_mount_align_real_onstep_adapter import _drain, _Rig


@pytest.fixture
def rig() -> _Rig:
    return _Rig()


def _dec_degrees(rig: _Rig) -> list[float]:
    return [deg for axis, deg in rig.client.axis_move_calls if axis == "dec"]


def test_the_default_step_angles_are_1_5_and_20_degrees() -> None:
    settings = MountAlignmentSettings()
    assert settings.nudge_fixed_angles is True
    assert [nudge_angle_arcsec(settings, size) / 3600.0 for size in MovementSize] == [
        1.0, 5.0, 20.0,
    ]


@pytest.mark.parametrize(("size", "parts"), [("SMALL", [1.0]), ("MEDIUM", [5.0])])
def test_small_and_medium_dec_steps_are_one_move(
    qapp: object, rig: _Rig, size: str, parts: list[float]
) -> None:
    panel = rig.panel()
    panel._size_buttons[MovementSize[size]].setChecked(True)
    panel._on_nudge_clicked("left", MountAxis.AXIS2, AxisDirection.POSITIVE)
    _drain(panel)
    assert _dec_degrees(rig) == [pytest.approx(deg) for deg in parts], panel._result_label.text()
    panel.stop()


def test_a_large_dec_step_is_20_degrees_in_two_parts(qapp: object, rig: _Rig) -> None:
    panel = rig.panel()  # Large is the default Move size
    panel._on_nudge_clicked("right", MountAxis.AXIS2, AxisDirection.NEGATIVE)
    _drain(panel)
    assert _dec_degrees(rig) == [pytest.approx(-10.0), pytest.approx(-10.0)], (
        panel._result_label.text()
    )
    panel.stop()


def test_a_large_ra_step_is_20_degrees_on_the_sky(qapp: object, rig: _Rig) -> None:
    panel = rig.panel()
    panel._on_nudge_clicked("left", MountAxis.AXIS2, AxisDirection.POSITIVE)  # Dec first
    _drain(panel)
    rig.client.axis_move_calls.clear()
    rig.client.axis_move_decs.clear()
    panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
    _drain(panel)
    ra = [(axis, deg) for axis, deg in rig.client.axis_move_calls if axis == "ra"]
    assert len(ra) == 2, panel._result_label.text()
    assert sum(rig.client.sky_move_arcsec()) / 3600.0 == pytest.approx(20.0)
    assert sum(deg for _a, deg in ra) == pytest.approx(
        20.0 / math.cos(math.radians(rig.client.axis_move_decs[0]))
    )
    panel.stop()
