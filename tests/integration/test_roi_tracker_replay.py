"""Stage 3 golden-master test: replays a scripted star-motion sequence
through camera -> detector -> RoiTracker and reproduces the architecture
doc's own example transition sequence:
[LOCKED, LOCKED, LOST, SEARCHING, REACQUIRED].

This is the first end-to-end proof that the shared astrotool_core pieces
compose correctly (per PLAN.md Stage 3's "done when"). Frames are pulled
synchronously via CameraPort.capture() rather than through
StreamController's background thread: StreamController's single-slot
mailbox is deliberately lossy (it exists to always hand a live guide
camera's *newest* frame to a consumer, dropping stale ones under load —
see acquisition/stream_controller.py and its own tests), which races
against a zero-delay replay producer and would make this determinism
test flaky. StreamController's streaming/dropping behavior itself is
covered separately by tests/core/acquisition/test_stream_controller.py.
"""

from __future__ import annotations

from pathlib import Path

from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker
from astrotool_core.testing.replay_dataset import load_expected

DATASET_DIR = (
    Path(__file__).resolve().parents[2] / "datasets" / "collimation" / "mono_adjustment_shift"
)


def test_roi_tracker_reproduces_the_doc_example_sequence() -> None:
    camera = ReplayCamera.from_directory(DATASET_DIR / "frames", cycle=False)
    camera.connect()

    first_frame = camera.capture(1.0)
    first_detection = detect_sources(first_frame.pixels)
    target = select_target(first_detection)
    assert target is not None, "no target found in the acquisition frame"

    tracker = RoiTracker(lock_tolerance_px=8.0, search_radius_px=60.0, lost_to_searching_frames=1)
    tracker.acquire(target.x, target.y)

    observed_states = []
    for _ in range(5):
        frame = camera.capture(1.0)
        detection = detect_sources(frame.pixels)
        result = tracker.update(detection.sources)
        observed_states.append(result.state.name)

    camera.disconnect()

    expected = load_expected(DATASET_DIR)
    assert observed_states == expected["lock_states"]
