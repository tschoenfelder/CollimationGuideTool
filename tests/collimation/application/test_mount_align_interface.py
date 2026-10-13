"""S6.6a: Mount Align availability (#42), the nudge confirmation, the abort return and the
calibration labels -- moved out of `MountTestMovePanel` unchanged. Tested directly (no panel,
no MainWindow, no Qt)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, AxisResponse, MountAxis
from collimation_tool.application.mount_align_failures import MeasurementFailureClass
from collimation_tool.application.mount_align_interface import mount_interface_state
from collimation_tool.application.mount_align_messages import calibration_progress_text
from collimation_tool.application.mount_align_plan import CALIBRATION_STEPS, step_label
from collimation_tool.application.mount_align_positioning import check_nudge_motion
from collimation_tool.application.mount_align_sizing import CalibrationSizing

pytestmark = pytest.mark.unit

A1 = MountAxis.AXIS1
POS, NEG = AxisDirection.POSITIVE, AxisDirection.NEGATIVE


@dataclass
class _ParkStatus:
    available: bool
    fresh: bool = True


@dataclass
class _PulseStatus:
    connected: bool


class _Park:
    def __init__(self, status: _ParkStatus) -> None:
        self._status = status

    def status(self) -> _ParkStatus:
        return self._status


class _Mount:
    def __init__(self, connected: bool, *, pending: bool = False) -> None:
        self._connected = connected
        self.status_pending = pending

    def status(self) -> _PulseStatus:
        return _PulseStatus(self._connected)


def _state(connected: bool, park: _ParkStatus, mount: _Mount) -> str:
    port_park: Any = _Park(park)
    port_mount: Any = mount
    return mount_interface_state(connected=connected, mount_park=port_park, mount=port_mount).code


def test_the_interface_state_codes() -> None:
    assert _state(False, _ParkStatus(True), _Mount(True)) == "not_connected"
    assert _state(True, _ParkStatus(False, fresh=False), _Mount(True)) == "mount_busy"
    assert _state(True, _ParkStatus(False), _Mount(True)) == "park_interface_unavailable"
    assert _state(True, _ParkStatus(True), _Mount(False, pending=True)) == "mount_busy"
    assert _state(True, _ParkStatus(True), _Mount(False)) == "pulse_interface_unavailable"
    assert _state(True, _ParkStatus(True), _Mount(True)) == "ok"


def _response(dx: float) -> AxisResponse:
    return AxisResponse(A1, POS, 500, dx, 0.0, dx / 500)


def test_a_nudge_check_confirms_each_camera_independently() -> None:
    before = {"left": (0.0, 0.0), "right": (0.0, 0.0)}
    results = {"left": _response(10.0), "right": None}
    check = check_nudge_motion(
        before,
        {"left": (10.0, 0.0), "right": (0.0, 0.0)},
        lambda b, a: results["left" if a == (10.0, 0.0) else "right"],
        missing=lambda: pytest.fail("frames were there"),
    )
    assert set(check.responses) == {"left"}
    assert check.failure_classes == {"right": MeasurementFailureClass.MATCH_FAILED}
    assert check.warning is None  # an unconfirmed camera is not "nothing moved"
    assert check.note == (
        " Main: dx=+10.0px dy=+0.0px (10.0px @ 0°) | Guide: displacement not confirmed"
    )


def test_a_nudge_check_warns_only_when_nothing_moved() -> None:
    """Issue #43 after the user decision: no motion is still detected and reported -- as a
    warning added after the move, never as a confirmed displacement."""
    check = check_nudge_motion(
        {"left": (0.0, 0.0)},
        {"left": (0.2, 0.0), "right": (1.0, 1.0)},
        lambda b, a: _response(0.2),
        missing=lambda: pytest.fail("frames were there"),
    )
    assert check.responses == {}
    assert check.failure_classes == {"left": MeasurementFailureClass.NO_MOTION_DETECTED}
    assert check.warning is not None and "no image motion measured on Main" in check.warning
    assert check.note == f" {check.warning} | Guide: displacement not confirmed"


def test_a_nudge_check_without_a_reference_or_frames_says_so() -> None:
    skipped = check_nudge_motion({}, {"left": (0.0, 0.0)}, lambda b, a: None, missing=str)
    assert (skipped.warning, skipped.failure_classes) == (None, {})
    assert "no verified at-rest view" in skipped.note
    failed = check_nudge_motion(
        {"left": (0.0, 0.0)}, None, lambda b, a: None, missing=lambda: "no frame after the move"
    )
    assert failed.warning is None
    assert failed.note == " (no-motion check not possible: no frame after the move)"


def test_an_aborted_run_is_returned_in_capped_chunks() -> None:
    settings = MountAlignmentSettings()
    sizing = CalibrationSizing(mount=lambda: object(), settings=lambda: settings)
    assert sizing.abort_return(A1, 3000) == ([], "")
    sizing.account_pulse(CALIBRATION_STEPS[0], 4000)
    motion, suffix = sizing.abort_return(A1, 3000)
    assert motion == [(A1, NEG, 3000), (A1, NEG, 1000)]
    assert suffix == " (returning mount to start position…)"


def test_calibration_labels_and_progress_text() -> None:
    step = CALIBRATION_STEPS[2]
    assert step_label(step, "before") == "axis1_negative_first_before"
    assert step_label(step, "after") == "axis1_negative_first_after"
    assert calibration_progress_text(A1, "NEGATIVE", "first", 800, None) == (
        "Calibrating AXIS1 (negative, first, 800 ms)…"
    )
    assert calibration_progress_text(A1, "POSITIVE", "repeat", 800, 61.4) == (
        "Calibrating AXIS1 (positive, repeat, 61 arcsec angular)…"
    )
