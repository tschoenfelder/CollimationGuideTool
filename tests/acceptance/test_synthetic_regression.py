from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astrotool_core.frames.analysis_plane import build_analysis_plane
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState
from astrotool_core.testing.frame_factory import donut_image, make_frame
from collimation_tool.application.collimation_controller import CollimationController
from collimation_tool.application.recenter_policy import CollimationRecenterPolicy, RecenterConfig
from collimation_tool.domain.collimation_measurement import DonutAnalyzer

DATA = Path(__file__).resolve().parents[2] / "datasets" / "acceptance"

def load(name: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads((DATA / name).read_text(encoding="utf-8"))
    return result

def by_id(doc: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(c for c in doc["cases"] if c["id"] == case_id)

COL = load("collimation_cases.json")
REC = load("mount_recenter_cases.json")

@pytest.mark.parametrize("case_id", [c["id"] for c in COL["cases"]])
def test_collimation_regression(case_id: str) -> None:
    case = by_id(COL, case_id)
    cfg = COL["frame"]
    shape = (cfg["height"], cfg["width"])
    if case.get("kind") == "uniform":
        pixels = np.full(shape, cfg["background_adu"], dtype=np.float32)
    else:
        pixels = donut_image(
            shape,
            outer_center=tuple(case["outer_center_px"]),
            outer_radius=cfg["outer_radius_px"],
            inner_center=tuple(case["inner_center_px"]),
            inner_radius=cfg["inner_radius_px"],
            peak=cfg["peak_adu"],
            background=cfg["background_adu"],
        )

    result, recommendation = CollimationController().measure_and_advise(
        build_analysis_plane(make_frame(pixels, bit_depth=cfg["bit_depth"]))
    )
    assert result.reason == case["expected_reason"]

    if result.reason != "ok":
        assert result.measurement is None
        assert recommendation is None
        return

    m = result.measurement
    assert m is not None
    ex, ey = case["expected_error_px"]
    tol = case["error_tolerance_px"]
    assert m.error_x_px == pytest.approx(ex, abs=tol)
    assert m.error_y_px == pytest.approx(ey, abs=tol)
    assert m.is_collimated is case["expected_collimated"]

def test_increasing_known_offset_increases_measured_error() -> None:
    mags = []
    cfg = COL["frame"]
    for case_id in ("small_error_1px", "medium_error_5px", "large_error_12px"):
        case = by_id(COL, case_id)
        pixels = donut_image(
            (cfg["height"], cfg["width"]),
            outer_center=tuple(case["outer_center_px"]),
            outer_radius=cfg["outer_radius_px"],
            inner_center=tuple(case["inner_center_px"]),
            inner_radius=cfg["inner_radius_px"],
            peak=cfg["peak_adu"],
            background=cfg["background_adu"],
        )
        result = DonutAnalyzer().analyze(build_analysis_plane(make_frame(pixels)))
        assert result.measurement is not None
        mags.append(result.measurement.error_magnitude_px)
    assert mags[0] < mags[1] < mags[2]

def calibration() -> CalibrationMatrix:
    responses = {}
    for key, motion in REC["motion_model"].items():
        axis_name, direction_name = key.rsplit("_", 1)
        axis = MountAxis[axis_name]
        direction = AxisDirection[direction_name]
        duration = 500
        dx = motion["dx_px_per_ms"] * duration
        dy = motion["dy_px_per_ms"] * duration
        responses[(axis, direction)] = AxisResponse(
            axis=axis, direction=direction, duration_ms=duration,
            dx_px=dx, dy_px=dy, px_per_ms=(dx * dx + dy * dy) ** 0.5 / duration,
        )
    return CalibrationMatrix(responses=responses)

class SyntheticOnStepMount:
    """Stateful test double: accepted pulses move the synthetic image target."""

    def __init__(self, start: tuple[float, float]) -> None:
        self.x, self.y = start
        self.connected = True
        self.pulse_log: list[tuple[MountAxis, AxisDirection, int]] = []

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9999)

    def status(self) -> MountStatus:
        return MountStatus(connected=self.connected, tracking=self.connected, slewing=False)

    def pulse_axis(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int
    ) -> CommandResult:
        if not self.connected:
            return CommandResult(accepted=False, message="synthetic mount disconnected")
        self.pulse_log.append((axis, direction, duration_ms))
        motion = REC["motion_model"][f"{axis.name}_{direction.name}"]
        self.x += motion["dx_px_per_ms"] * duration_ms
        self.y += motion["dy_px_per_ms"] * duration_ms
        return CommandResult(accepted=True)

def measure_donut_center(mount: SyntheticOnStepMount) -> TrackingResult:
    cfg = REC["donut"]
    center = (mount.x, mount.y)
    pixels = donut_image(
        (240, 240),
        outer_center=center, outer_radius=cfg["outer_radius_px"],
        inner_center=center, inner_radius=cfg["inner_radius_px"],
        peak=cfg["peak_adu"], background=cfg["background_adu"],
    )
    result = DonutAnalyzer().analyze(
        build_analysis_plane(make_frame(pixels, bit_depth=cfg["bit_depth"]))
    )
    if result.measurement is None:
        return TrackingResult(state=TrackingState.LOST, x=None, y=None, matched_source=None)
    outer = result.measurement.outer_ring
    return TrackingResult(
        state=TrackingState.LOCKED, x=outer.center_x, y=outer.center_y, matched_source=None
    )

@pytest.mark.parametrize("case_id", [c["id"] for c in REC["cases"]])
def test_off_center_donut_drives_mount_and_converges(case_id: str) -> None:
    case = by_id(REC, case_id)
    mount = SyntheticOnStepMount(tuple(case["start_center_px"]))
    policy = CollimationRecenterPolicy(
        mount, calibration(),
        RecenterConfig(settle_ms=0, fine_tolerance_px=1.5, max_pulse_ms=500),
    )
    reference = tuple(REC["frame_center_px"])

    result = policy.center(lambda: measure_donut_center(mount), reference=reference)
    assert result.success is case["expected_success"]

    expected = [
        (MountAxis[a], AxisDirection[d], ms)
        for a, d, ms in case["expected_pulses"]
    ]
    assert mount.pulse_log == expected

    final = measure_donut_center(mount)
    assert final.x == pytest.approx(reference[0], abs=1.0)
    assert final.y == pytest.approx(reference[1], abs=1.0)

    before = len(mount.pulse_log)
    second = policy.center(lambda: measure_donut_center(mount), reference=reference)
    assert second.success is True
    assert len(mount.pulse_log) == before
