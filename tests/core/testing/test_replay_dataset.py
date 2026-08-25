import json
from pathlib import Path

import pytest
from astrotool_core.testing.frame_factory import make_frame, single_star_image
from astrotool_core.testing.replay_dataset import discover_fits_paths, load_expected, load_frames


def test_discover_fits_paths_prefers_a_frames_subdirectory(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    (frames_dir / "a.fits").write_bytes(b"")
    (tmp_path / "not_a_frame.fits").write_bytes(b"")

    paths = discover_fits_paths(tmp_path)
    assert len(paths) == 1
    assert paths[0].parent == frames_dir


def test_discover_fits_paths_falls_back_to_dataset_dir_itself(tmp_path: Path) -> None:
    (tmp_path / "a.fits").write_bytes(b"")
    (tmp_path / "b.fit").write_bytes(b"")

    paths = discover_fits_paths(tmp_path)
    assert len(paths) == 2


def test_load_frames_raises_when_no_fits_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_frames(tmp_path)


def test_load_frames_reads_saved_frames_in_order(tmp_path: Path) -> None:
    frames_dir = tmp_path / "frames"
    frames_dir.mkdir()
    frame_a = make_frame(single_star_image((16, 16), x=4, y=4, peak=500.0))
    frame_b = make_frame(single_star_image((16, 16), x=10, y=10, peak=800.0))
    (frames_dir / "0_a.fits").write_bytes(frame_a.to_fits_bytes())
    (frames_dir / "1_b.fits").write_bytes(frame_b.to_fits_bytes())

    loaded = load_frames(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].pixels[4, 4] > loaded[0].pixels[10, 10]
    assert loaded[1].pixels[10, 10] > loaded[1].pixels[4, 4]


def test_load_expected_raises_when_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_expected(tmp_path)


def test_load_expected_reads_json(tmp_path: Path) -> None:
    (tmp_path / "expected.json").write_text(json.dumps({"lock_states": ["LOCKED"]}))
    expected = load_expected(tmp_path)
    assert expected == {"lock_states": ["LOCKED"]}
