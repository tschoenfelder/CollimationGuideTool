from datetime import UTC, datetime
from pathlib import Path

from astropy.io import fits
from astrotool_core.session.frame_recorder import make_filename, save_frame
from astrotool_core.testing.frame_factory import make_frame, single_star_image


def test_make_filename_contains_all_key_fields() -> None:
    filename = make_filename(
        timestamp=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        session_id="abcdefgh-1234",
        section="collimation",
        run_id="run12345",
        iteration=2,
        camera_id="CAM-1",
        exposure_s=1.5,
        gain=100,
        offset=10,
    )
    assert filename.startswith("20260102T030405")
    assert "session-abcdefgh" in filename
    assert "collimation" in filename
    assert "run-run1234" in filename or "run1234" in filename
    assert "iter-2" in filename
    assert "CAM-1" in filename
    assert "exp-1.500s" in filename
    assert "gain-100" in filename
    assert "offset-10" in filename
    assert filename.endswith(".fits")


def test_save_frame_writes_a_readable_fits_file(tmp_path: Path) -> None:
    frame = make_frame(single_star_image((16, 16), x=8, y=8, peak=500.0), exposure_seconds=0.5)
    path = save_frame(
        frame,
        tmp_path,
        session_id="12345678",
        section="camera",
        run_id="run00001",
        camera_id="fake",
        gain=100,
        offset=0,
    )
    assert path.exists()
    with fits.open(str(path)) as hdul:
        assert hdul[0].data.shape == (16, 16)
        assert hdul[0].header["EXPTIME"] == 0.5
        assert hdul[0].header["SESSION"] == "12345678"
        assert hdul[0].header["SECTION"] == "camera"


def test_save_frame_places_file_under_session_slug_directory(tmp_path: Path) -> None:
    frame = make_frame(single_star_image((8, 8), x=4, y=4, peak=100.0))
    path = save_frame(
        frame,
        tmp_path,
        session_id="fullsession-id-1234",
        section="camera",
        run_id="run00001",
    )
    assert path.parent.name == "fullsession-id-1234"[:8]
