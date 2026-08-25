"""ReplayDataset — loads a recorded FITS sequence (+ optional expected.json)
for golden-master/integration tests.

The FITS-discovery logic is ported from smart_telescope's
``adapters.replay.camera.ReplayCamera.from_directory``, but separated from
any CameraPort — this module only loads frames and expectations;
``camera/replay_camera.py`` wraps it as a CameraPort adapter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrotool_core.frames.frame import Frame

_FITS_GLOB = ("*.fits", "*.fit")


def discover_fits_paths(dataset_dir: Path | str) -> list[Path]:
    """Return sorted FITS file paths under *dataset_dir* (or its frames/ subdir)."""
    root = Path(dataset_dir)
    frames_dir = root / "frames"
    search_dir = frames_dir if frames_dir.is_dir() else root
    seen: set[Path] = set()
    for pattern in _FITS_GLOB:
        seen.update(search_dir.glob(pattern))
    return sorted(seen)


def load_frames(dataset_dir: Path | str) -> list[Frame]:
    """Load every FITS frame under *dataset_dir*, sorted by filename."""
    paths = discover_fits_paths(dataset_dir)
    if not paths:
        raise ValueError(f"replay_dataset: no FITS files found under {dataset_dir}")
    return [Frame.from_fits_bytes(path.read_bytes()) for path in paths]


def load_expected(dataset_dir: Path | str) -> dict[str, Any]:
    """Load expected.json from *dataset_dir*, for golden-master tolerance checks."""
    path = Path(dataset_dir) / "expected.json"
    if not path.exists():
        raise FileNotFoundError(f"replay_dataset: no expected.json under {dataset_dir}")
    result: dict[str, Any] = json.loads(path.read_text())
    return result
