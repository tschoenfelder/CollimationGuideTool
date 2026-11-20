"""Operator request (2026-11-18): on a mount with angular moves the RA/Dec buttons move FIXED
angles on the sky -- Small 1, Medium 5, Large 20 degrees -- whatever the camera optics or
calibration. A 20 degree step exceeds OnStepAdapter's 15 degree single-move bound, so the
production `OnStepMountPulseAdapter` runs it as equal parts."""

from __future__ import annotations

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


def test_a_large_dec_step_is_one_20_degree_move(qapp: object, rig: _Rig) -> None:
    """OnStepAdapter 0.7.17 runs a finite move of up to 45 degrees (field capture 2eb2a5cb:
    two 10 degree parts doubled the time of a 20 degree step)."""
    panel = rig.panel()  # Large is the default Move size
    panel._on_nudge_clicked("right", MountAxis.AXIS2, AxisDirection.NEGATIVE)
    _drain(panel)
    assert _dec_degrees(rig) == [pytest.approx(-20.0)], panel._result_label.text()
    panel.stop()


def test_a_large_ra_step_turns_the_ra_axis_20_degrees_at_any_dec(qapp: object, rig: _Rig) -> None:
    """Operator (2026-11-18, diag fd84c75f): the fixed 1/5/20 degree buttons turn the axis
    itself -- at Dec 71.9 a 20 degree step "on the sky" needed a 64 degree RA-axis turn and was
    refused. One move, 20 degrees of RA axis, whatever the Dec."""
    panel = rig.panel()
    panel._on_nudge_clicked("left", MountAxis.AXIS2, AxisDirection.POSITIVE)  # Dec first
    _drain(panel)
    rig.client.axis_move_calls.clear()
    rig.client.axis_move_decs.clear()
    rig.client.dec_deg = 71.9
    panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
    _drain(panel)
    ra = [(axis, deg) for axis, deg in rig.client.axis_move_calls if axis == "ra"]
    assert [deg for _a, deg in ra] == [pytest.approx(20.0)], panel._result_label.text()
    panel.stop()


def test_a_fixed_step_waits_no_extra_settle_time(
    qapp: object, rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator (2026-11-18 21:00): a Small step took too long until the buttons were free
    again. OnStepAdapter returns only once the axis is at rest; a fixed (positioning) step adds
    no `settle_ms` on top."""
    import dataclasses

    from collimation_tool.application.mount_align_motion import submit_axis_move
    from collimation_tool.ui import mount_test_move_panel

    calls: list[dict[str, object]] = []

    def spy(*args: object, **kwargs: object) -> bool:
        calls.append(kwargs)
        return bool(submit_axis_move(*args, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(mount_test_move_panel, "submit_axis_move", spy)
    panel = rig.panel()
    panel._settings = dataclasses.replace(panel._settings, settle_ms=1000)  # the default
    panel._size_buttons[MovementSize.SMALL].setChecked(True)
    panel._on_nudge_clicked("left", MountAxis.AXIS2, AxisDirection.POSITIVE)
    _drain(panel)
    assert calls and calls[0]["settle_ms"] == 0 and calls[0]["axis_angle"] is True
    panel.stop()


def test_the_move_size_buttons_show_their_angles(qapp: object, rig: _Rig) -> None:
    """Operator (2026-11-20): "Change buttons small, medium large to the degree values"."""
    panel = rig.panel()
    labels = [panel._size_buttons[size].text() for size in MovementSize]
    assert labels == ["1°", "5°", "20°"]
    panel.stop()
