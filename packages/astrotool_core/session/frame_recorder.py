"""FrameRecorder — writes captured Frames to disk as diagnostic FITS files.

Ported from smart_telescope's ``services.diagnostic_frame_store.DiagnosticFrameStore``,
trimmed to what this project's smaller MountPort can actually supply:
RA/Dec/tracking/optical-train headers are dropped (MountStatus carries no
sky position — see mount/port.py's Stage 2 trim), keeping only what's
always known: session, section, run, camera identity, exposure/gain/
offset, bit depth, timestamp. Pairs with ``testing.replay_dataset`` (the
reader) for golden-master datasets.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from astropy.io import fits

from astrotool_core.frames.frame import Frame


def _safe(value: str, max_len: int = 32) -> str:
    """Replace filesystem-unsafe characters and truncate."""
    out = value.replace("/", "-").replace("\\", "-").replace(":", "-").replace(" ", "_")
    return out[:max_len]


def make_filename(
    *,
    timestamp: datetime,
    session_id: str,
    section: str,
    run_id: str,
    iteration: int,
    camera_id: str,
    exposure_s: float,
    gain: int,
    offset: int,
) -> str:
    """Build the standardized diagnostic FITS filename.

    Pattern::

        YYYYMMDDTHHMMSS_session-<id>_<section>_<run_id>_iter-<n>_<camera_id>
        _exp-<s>s_gain-<g>_offset-<o>.fits
    """
    date_part = timestamp.strftime("%Y%m%dT%H%M%S")
    parts = [
        date_part,
        f"session-{_safe(session_id[:8])}",
        _safe(section),
        _safe(run_id[:8]),
        f"iter-{iteration}",
        _safe(camera_id),
        f"exp-{exposure_s:.3f}s",
        f"gain-{gain}",
        f"offset-{offset}",
    ]
    return "_".join(parts) + ".fits"


def save_frame(
    frame: Frame,
    dest_dir: Path | str,
    *,
    session_id: str,
    section: str,
    run_id: str,
    iteration: int = 0,
    camera_id: str = "unknown",
    gain: int = 0,
    offset: int = 0,
    timestamp: datetime | None = None,
) -> Path:
    """Save *frame* as a FITS file with standardized diagnostic headers.

    Returns the path to the saved file.
    """
    ts = timestamp or datetime.now(UTC)
    filename = make_filename(
        timestamp=ts,
        session_id=session_id,
        section=section,
        run_id=run_id,
        iteration=iteration,
        camera_id=camera_id,
        exposure_s=frame.exposure_seconds,
        gain=gain,
        offset=offset,
    )
    dest_root = Path(dest_dir) / session_id[:8]
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / filename

    header = fits.Header()
    header["SESSION"] = (session_id[:8], "Session ID (first 8 chars)")
    header["SECTION"] = (section, "Log section name")
    header["RUNID"] = (run_id[:8], "Run ID")
    header["ITER"] = (iteration, "0-based iteration index")
    header["CAMERA"] = (camera_id, "Camera identifier")
    header["EXPTIME"] = (frame.exposure_seconds, "Exposure time [s]")
    header["GAIN"] = (gain, "Camera gain")
    header["OFFSET"] = (offset, "Camera black level/offset")
    header["BITDEPTH"] = (frame.bit_depth, "Sensor bit depth")
    header["DATE-OBS"] = (ts.isoformat(), "Observation UTC timestamp")

    hdu = fits.PrimaryHDU(data=frame.pixels.astype(np.float32), header=header)
    hdu.writeto(str(dest), overwrite=True)
    return dest
