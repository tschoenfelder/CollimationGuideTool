"""Stage 6 golden-master test: replays a scripted lost/reacquired guide-star
sequence through detect_sources -> RoiTracker -> compute_guide_error,
proving the same shared astrotool_core tracking core Stage 3 exercised for
CollimationTool is genuinely shared with GuideTool's own error signal.

Frames are pulled synchronously via CameraPort.capture() rather than
through GuideController's background thread — same determinism reasoning
as Stage 3's test_roi_tracker_replay.py: StreamController's single-slot
mailbox is deliberately lossy and would race against a zero-delay replay
producer.
"""

from __future__ import annotations

from pathlib import Path

from _golden_master import assert_matches_golden
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker
from astrotool_core.testing.replay_dataset import load_expected
from guide_tool.domain.guide_error import compute_guide_error

DATASET_DIR = Path(__file__).resolve().parents[2] / "datasets" / "guiding" / "lost_star"


def test_guide_error_reproduces_lost_and_reacquired_sequence() -> None:
    camera = ReplayCamera.from_directory(DATASET_DIR / "frames", cycle=False)
    camera.connect()

    first_frame = camera.capture(1.0)
    first_detection = detect_sources(first_frame.pixels)
    target_source = select_target(first_detection)
    assert target_source is not None, "no target found in the acquisition frame"
    target = (target_source.x, target_source.y)

    tracker = RoiTracker()
    tracker.acquire(target_source.x, target_source.y)

    observed_states = []
    observed_errors = []
    for _ in range(4):
        frame = camera.capture(1.0)
        detection = detect_sources(frame.pixels)
        result = tracker.update(detection.sources)
        error = compute_guide_error(result, target)
        observed_states.append(result.state.name)
        observed_errors.append(
            {"error_x": error.error_x, "error_y": error.error_y} if error.accepted else None
        )

    camera.disconnect()

    expected = load_expected(DATASET_DIR)
    assert observed_states == expected["states"]
    for observed, expected_error in zip(observed_errors, expected["errors"], strict=True):
        if expected_error is None:
            assert observed is None
        else:
            assert observed is not None
            assert_matches_golden(
                observed, expected_error, tolerances={"error_x": 0.5, "error_y": 0.5}
            )
