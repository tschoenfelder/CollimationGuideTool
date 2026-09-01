from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.target.roi_tracker import RoiTracker
from astrotool_core.testing.frame_factory import single_star_image
from guide_tool.application.guide_controller import GuideController
from guide_tool.domain.correction_model import GuideCorrectionConfig

DATA = Path(__file__).resolve().parents[2] / "datasets" / "acceptance"
DOC: dict[str, Any] = json.loads((DATA / "guiding_cases.json").read_text(encoding="utf-8"))

def calibration(model_name: str) -> CalibrationMatrix:
    responses = {}
    for key, motion in DOC["motion_models"][model_name].items():
        axis_name, direction_name = key.rsplit("_", 1)
        axis = MountAxis[axis_name]
        direction = AxisDirection[direction_name]
        duration = 500
        dx = motion["dx_px_per_ms"] * duration
        dy = motion["dy_px_per_ms"] * duration
        responses[(axis, direction)] = AxisResponse(
            axis=axis, direction=direction, duration_ms=duration,
            dx_px=dx, dy_px=dy,
            px_per_ms=(dx * dx + dy * dy) ** 0.5 / duration,
        )
    return CalibrationMatrix(responses=responses)

class SyntheticGuideMount:
    def __init__(self, start: tuple[float, float], model_name: str) -> None:
        self.x, self.y = start
        self.model_name = model_name
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
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        if not self.connected:
            return CommandResult(accepted=False, message="synthetic mount disconnected")
        self.pulse_log.append((axis, direction, duration_ms))
        m = DOC["motion_models"][self.model_name][f"{axis.name}_{direction.name}"]
        self.x += m["dx_px_per_ms"] * duration_ms
        self.y += m["dy_px_per_ms"] * duration_ms
        return CommandResult(accepted=True)

def frame_at(position: tuple[float, float] | None) -> np.ndarray:
    cfg = DOC["frame"]
    shape = (cfg["height"], cfg["width"])
    if position is None:
        return np.full(shape, cfg["background_adu"], dtype=np.float32)
    return single_star_image(
        shape, x=position[0], y=position[1],
        peak=cfg["star_peak_adu"], sigma=cfg["star_sigma_px"],
        background=cfg["background_adu"],
    )

def correction_config() -> GuideCorrectionConfig:
    c = DOC["correction_config"]
    return GuideCorrectionConfig(
        deadband_px=c["deadband_px"], max_pulse_ms=c["max_pulse_ms"],
        min_pulse_ms=c["min_pulse_ms"], aggressiveness=c["aggressiveness"],
        axis2_enabled=c["axis2_enabled"],
    )

def make_controller(mount: SyntheticGuideMount, model_name: str) -> GuideController:
    # Camera is unused in this harness; process_frame consumes pixels directly and
    # start()/StreamController are never called, so any CameraPort stands in.
    return GuideController(
        FakeCamera(), mount=mount, calibration=calibration(model_name),
        correction_config=correction_config(), measure_only=False,
        tracker=RoiTracker(lock_tolerance_px=30.0),
    )

def acquire_target(controller: GuideController) -> None:
    target = tuple(DOC["target_px"])
    err = controller.process_frame(frame_at(target)).error
    assert err is not None
    assert err.accepted

@pytest.mark.parametrize("case", DOC["closed_loop_cases"], ids=lambda c: c["id"])
def test_guiding_closed_loop_moves_star_back_to_target(case: dict[str, Any]) -> None:
    model = case["motion_model"]
    mount = SyntheticGuideMount(tuple(case["start_px"]), model)
    controller = make_controller(mount, model)
    acquire_target(controller)

    # First post-acquisition frame contains the simulated drift.
    result = controller.process_frame(frame_at((mount.x, mount.y)))
    error = result.error
    assert error is not None and error.accepted
    before_mag = error.error_magnitude_px
    assert before_mag is not None and before_mag > 0

    expected = [(MountAxis[a], AxisDirection[d], ms) for a, d, ms in case["expected_pulses"]]
    assert mount.pulse_log == expected

    # The next image is rendered from the moved mount state, then measured for real.
    after_result = controller.process_frame(frame_at((mount.x, mount.y)))
    after = after_result.error
    assert after is not None and after.accepted
    assert after.error_magnitude_px is not None
    assert after.error_magnitude_px < before_mag
    assert after.error_magnitude_px <= 1.0

    ex, ey = case["expected_final_px"]
    assert mount.x == pytest.approx(ex, abs=0.01)
    assert mount.y == pytest.approx(ey, abs=0.01)

    # Once corrected, another frame must not command another pulse.
    controller.process_frame(frame_at((mount.x, mount.y)))
    assert mount.pulse_log == expected

@pytest.mark.parametrize("case", DOC["no_correction_cases"], ids=lambda c: c["id"])
def test_guiding_inside_deadband_does_not_move_mount(case: dict[str, Any]) -> None:
    mount = SyntheticGuideMount(tuple(case["position_px"]), "normal")
    controller = make_controller(mount, "normal")
    acquire_target(controller)
    result = controller.process_frame(frame_at(tuple(case["position_px"])))
    assert result.error is not None and result.error.accepted
    assert mount.pulse_log == []

def test_reversed_camera_orientation_uses_calibration_not_assumed_sign() -> None:
    case = next(c for c in DOC["closed_loop_cases"] if c["id"] == "reversed_camera_right_20px")
    mount = SyntheticGuideMount(tuple(case["start_px"]), "reversed_axis1")
    controller = make_controller(mount, "reversed_axis1")
    acquire_target(controller)
    result = controller.process_frame(frame_at((mount.x, mount.y)))
    assert result.error is not None and result.error.accepted
    pulses = result.pulses
    assert len(pulses) == 1
    assert pulses[0].axis is MountAxis.AXIS1
    # With reversed image response POSITIVE is the corrective direction for a +X image error.
    assert pulses[0].direction is AxisDirection.POSITIVE
    assert mount.x == pytest.approx(DOC["target_px"][0], abs=0.01)

def test_lost_guide_star_never_generates_mount_pulse_and_can_reacquire() -> None:
    mount = SyntheticGuideMount(tuple(DOC["target_px"]), "normal")
    controller = make_controller(mount, "normal")
    acquire_target(controller)

    positions = DOC["lost_reacquired"]["positions"][1:]
    pulse_counts = []
    accepted_after_loss = False
    for pos in positions:
        position = None if pos is None else tuple(pos)
        result = controller.process_frame(frame_at(position))
        if position is None:
            assert result.pulses == []
        else:
            if result.error is not None and result.error.accepted:
                accepted_after_loss = True
        pulse_counts.append(len(mount.pulse_log))

    # Missing frames themselves never cause a correction.
    assert pulse_counts[1] == pulse_counts[0]
    assert pulse_counts[2] == pulse_counts[1]
    # The tracker ultimately accepts a real source again.
    assert accepted_after_loss is True
