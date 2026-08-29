"""Reads optical-train plate scale from the sibling SmartTScope project's
config.toml — the file that actually records this rig's telescope focal
lengths and camera-to-telescope bindings ([telescopes]/[optical_trains]/
[cameras]). CollimationGuideTool has no config system of its own yet (see
install.md) and duplicating that schema would just create a second,
driftable source of truth for the same physical hardware — so this reads
SmartTScope's file directly instead, read-only, at call time.

Deliberately narrow: answers exactly one question ("what's this optical
train's plate scale, in arcsec/px?"), not a general SmartTScope config
model. Used for the main-camera-FOV-in-guide-frame overlay — see
`collimation_tool.ui.fov_overlay`.

Every failure mode here (file missing, train not found, telescope not
found, no usable plate scale) returns None rather than raising: this
file lives outside CollimationGuideTool and is optional — not every
install has SmartTScope alongside it, and a missing overlay is a
graceful no-op, never a hard error.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path.home() / ".SmartTScope" / "config.toml"

#: 206264.8 arcsec/radian — the standard plate-scale constant:
#: arcsec_per_px = ARCSEC_PER_RADIAN * (pixel_size_mm / focal_length_mm).
_ARCSEC_PER_RADIAN = 206264.8

# SmartTScope's config.toml doesn't currently record each camera model's
# physical pixel size as a live setting — e.g. [optical_trains.guide]'s
# pixel_scale_arcsec line is commented out, with only a documentation
# comment ("guide scope 50/180 + 2.9 µm sensor") giving the assumption
# behind it. This is that same assumption, made explicit here instead of
# buried in a comment. Extend this if a new camera model needs the same
# treatment, or better: once astrotool_core.camera.CameraCapabilities.
# pixel_size_um is actually populated from hardware (currently always
# 0.0 — see touptek_adapter.py), prefer that live value over this table.
KNOWN_PIXEL_SIZE_UM: dict[str, float] = {
    "GPCMOS02000KPA": 2.9,
}


def _pixel_scale_arcsec(*, focal_mm: float, pixel_size_um: float) -> float:
    return _ARCSEC_PER_RADIAN * (pixel_size_um / 1000.0) / focal_mm


def _load_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
            return data
    except (OSError, tomllib.TOMLDecodeError):
        return None


def load_pixel_scale_arcsec(
    train_name: str, *, config_path: Path | str = DEFAULT_CONFIG_PATH
) -> float | None:
    """Plate scale (arcsec/px) for one `[optical_trains.<train_name>]` entry.

    Resolution order, matching config.toml's own documented convention
    ("optional override; computed from focal_mm if omitted"):
    1. An explicit `pixel_scale_arcsec` on the train itself.
    2. `[session] pixel_scale_arcsec` — SmartTScope's own single active
       override, documented there as being for the main imaging train's
       focal length (e.g. "C8 native = 0.38"); used only for the "main"
       train.
    3. Computed from the train's telescope focal_mm (× reducer_factor)
       and its camera model's pixel size (`KNOWN_PIXEL_SIZE_UM`).

    Returns None if any required piece is missing.
    """
    data = _load_toml(Path(config_path))
    if data is None:
        return None

    train = data.get("optical_trains", {}).get(train_name)
    if not isinstance(train, dict):
        return None

    override = train.get("pixel_scale_arcsec")
    if override is not None:
        return float(override)

    if train_name == "main":
        session_override = data.get("session", {}).get("pixel_scale_arcsec")
        if session_override is not None:
            return float(session_override)

    telescope_name = train.get("telescope")
    telescope = data.get("telescopes", {}).get(telescope_name)
    if not isinstance(telescope, dict):
        return None
    focal_mm = telescope.get("focal_mm")
    if not focal_mm:
        return None
    reducer_factor = float(train.get("reducer_factor", 1.0))
    effective_focal_mm = float(focal_mm) * reducer_factor

    camera_role = train.get("camera")
    camera = data.get("cameras", {}).get(camera_role)
    camera_model = camera.get("model") if isinstance(camera, dict) else None
    pixel_size_um = KNOWN_PIXEL_SIZE_UM.get(camera_model) if camera_model else None
    if pixel_size_um is None:
        return None

    return _pixel_scale_arcsec(focal_mm=effective_focal_mm, pixel_size_um=pixel_size_um)
