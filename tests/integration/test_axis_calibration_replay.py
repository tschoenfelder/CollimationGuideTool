"""Stage 4 golden-master test: axis-calibration pulse/response, replayed
against synthetic before/after frame pairs, reproduces the expected px/ms
response matrix within tolerance.

Demonstrates axis_calibration.py's intended real-world composition: the
``measure`` callback captures a frame, detects sources, and reports a
position via RoiTracker — re-acquiring fresh at each "before" step (a
deliberate large commanded pulse is not something a live-guiding lock
tolerance should be expected to track through in one update).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _golden_master import assert_matches_golden
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.mount.axis_calibration import calibrate_axes
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.replay_dataset import load_expected

DATASETS_DIR = Path(__file__).resolve().parents[2] / "datasets" / "guiding"

_AXIS_BY_DATASET = {
    "axis1_response": MountAxis.AXIS1,
    "axis2_response": MountAxis.AXIS2,
}


@pytest.mark.parametrize("dataset_name", ["axis1_response", "axis2_response"])
def test_axis_calibration_reproduces_expected_response_matrix(dataset_name: str) -> None:
    dataset_dir = DATASETS_DIR / dataset_name
    axis = _AXIS_BY_DATASET[dataset_name]

    camera = ReplayCamera.from_directory(dataset_dir / "frames", cycle=False)
    camera.connect()
    tracker = RoiTracker(lock_tolerance_px=30.0)
    call_index = 0

    def measure() -> tuple[float, float]:
        nonlocal call_index
        frame = camera.capture(1.0)
        detection = detect_sources(frame.pixels)
        # Even calls (0, 2) are "before" frames: fresh acquisition. Odd
        # calls (1, 3) are "after" frames: measured via the tracker.
        if call_index % 2 == 0:
            target = select_target(detection)
            assert target is not None, "no target found in the before-pulse frame"
            result = tracker.acquire(target.x, target.y)
        else:
            result = tracker.update(detection.sources)
        call_index += 1
        assert result.x is not None
        assert result.y is not None
        return (result.x, result.y)

    mount = FakeMountAdapter()
    mount.connect()

    expected = load_expected(dataset_dir)
    matrix = calibrate_axes(
        mount,
        measure=measure,
        pulse_ms=expected["pulse_ms"],
        axes=(axis,),
        directions=(AxisDirection.POSITIVE, AxisDirection.NEGATIVE),
    )

    for key, expected_response in expected["responses"].items():
        axis_name, direction_name = key.split("_")
        response = matrix.response_for(MountAxis[axis_name], AxisDirection[direction_name])
        actual = {
            "dx_px": response.dx_px,
            "dy_px": response.dy_px,
            "px_per_ms": response.px_per_ms,
        }
        assert_matches_golden(
            actual,
            expected_response,
            tolerances={"dx_px": 0.5, "dy_px": 0.5, "px_per_ms": 0.005},
        )
