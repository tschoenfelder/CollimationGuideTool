from pathlib import Path

import numpy as np
import pytest
from astrotool_core.camera.port import CaptureAbortedError
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.session.frame_recorder import save_frame
from astrotool_core.testing.frame_factory import make_frame, single_star_image


def test_requires_at_least_one_frame() -> None:
    with pytest.raises(ValueError):
        ReplayCamera([])


def test_from_arrays_cycles_through_frames_in_order() -> None:
    arrays = [
        single_star_image((16, 16), x=4, y=4, peak=500.0),
        single_star_image((16, 16), x=10, y=10, peak=800.0),
    ]
    camera = ReplayCamera.from_arrays(arrays)
    first = camera.capture(1.0)
    second = camera.capture(1.0)
    third = camera.capture(1.0)  # wraps around
    assert first.pixels[4, 4] > first.pixels[10, 10]
    assert second.pixels[10, 10] > second.pixels[4, 4]
    assert np.array_equal(third.pixels, first.pixels)


def test_capture_sets_requested_exposure_seconds() -> None:
    camera = ReplayCamera.from_arrays([np.zeros((4, 4), dtype=np.float32)])
    frame = camera.capture(2.5)
    assert frame.exposure_seconds == 2.5


def test_non_cycling_replay_raises_when_exhausted() -> None:
    camera = ReplayCamera.from_arrays([np.zeros((4, 4), dtype=np.float32)], cycle=False)
    camera.capture(1.0)
    with pytest.raises(CaptureAbortedError):
        camera.capture(1.0)


def test_reset_rewinds_to_the_first_frame() -> None:
    camera = ReplayCamera.from_arrays(
        [np.zeros((4, 4), dtype=np.float32), np.ones((4, 4), dtype=np.float32)]
    )
    camera.capture(1.0)
    assert camera.frame_index == 1
    camera.reset()
    assert camera.frame_index == 0


def test_from_directory_loads_saved_fits_frames_in_order(tmp_path: Path) -> None:
    frame_a = make_frame(single_star_image((16, 16), x=4, y=4, peak=500.0))
    frame_b = make_frame(single_star_image((16, 16), x=10, y=10, peak=800.0))
    # save_frame nests files under {dest_dir}/{session_id[:8]}/ — point
    # ReplayCamera.from_directory at that actual output directory.
    saved_a = save_frame(frame_a, tmp_path, session_id="s1", section="c", run_id="r1", iteration=0)
    save_frame(frame_b, tmp_path, session_id="s1", section="c", run_id="r1", iteration=1)

    camera = ReplayCamera.from_directory(saved_a.parent)
    first = camera.capture(1.0)
    second = camera.capture(1.0)
    assert first.pixels[4, 4] > first.pixels[10, 10]
    assert second.pixels[10, 10] > second.pixels[4, 4]


def test_from_directory_raises_when_empty(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ReplayCamera.from_directory(tmp_path)


def test_descriptor_and_setters_round_trip() -> None:
    camera = ReplayCamera.from_arrays([np.zeros((4, 4), dtype=np.float32)])
    camera.set_gain(500)
    assert camera.get_gain() == 500
    camera.set_black_level(20)
    assert camera.get_black_level() == 20
    assert camera.get_descriptor().logical_name == "ReplayCamera"
    assert camera.get_temperature() is None
