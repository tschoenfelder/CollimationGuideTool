"""Per-camera-panel settings, persisted so the last-connected camera and
its exposure/gain/auto-exposure state come back automatically on the next
launch.

Requested directly: "Add storing own settings for cameras connected as
the default startup settings. Assumption is, that the hardware will not
change each time" — a rig's cameras stay plugged into the same USB ports
between sessions far more often than not, so remembering last session's
choice (falling back to the demo camera if that device isn't currently
enumerated — see `apply_saved_settings`) saves re-picking it and re-tuning
exposure/gain by hand every startup.

Written to `[cameras.<panel_name>]` tables in the shared
`~/.CollimationGuideTool/config.toml` (see install.md's "Configuration"
section — this was the first thing to actually read/write that file;
`mount_alignment_settings.py`'s `[mount_alignment]` table is a second one
now sharing it). `save_camera_settings` only ever rewrites its own
`[cameras.*]` tables (see `_strip_table_blocks`) — a sibling table survives
untouched across every save here, and vice versa.

Hand-rolled TOML writing rather than a new dependency: stdlib `tomllib`
(used for reading) is read-only, and this schema is a small, fixed shape
(flat str/float/int/bool scalars under one table per panel) — well within
what's safe to serialize directly, the same reasoning `fov_registration`
uses to avoid adding OpenCV/SciPy for one FFT-based algorithm.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".CollimationGuideTool" / "config.toml"


@dataclass(frozen=True)
class CameraPanelSettings:
    """One panel's persisted state.

    ``camera_id`` is a `TouptekDeviceInfo.camera_id` (see
    `astrotool_core.camera.touptek_adapter`); ``None`` means the demo
    camera. Restoring a ``camera_id`` that isn't currently enumerated is
    a graceful no-op (stay on the demo camera), not an error — the
    "hardware will not change each time" assumption is a good bet, not a
    guarantee.
    """

    camera_id: str | None
    exposure_ms: float
    gain: int
    auto_exposure_enabled: bool


def load_camera_settings(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> dict[str, CameraPanelSettings]:
    """``{panel_name: CameraPanelSettings}`` for every ``[cameras.<panel_name>]``
    table found.

    A missing file, unreadable file, or malformed table is never an
    error — this is a convenience restore, not required state. A missing
    or bad entry for one panel is skipped on its own; it doesn't block
    restoring the other panel's settings.
    """
    settings: dict[str, CameraPanelSettings] = {}
    try:
        with Path(path).open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return settings

    cameras = data.get("cameras")
    if not isinstance(cameras, dict):
        return settings

    for panel_name, table in cameras.items():
        if not isinstance(table, dict):
            continue
        try:
            camera_id = table.get("camera_id") or None
            settings[panel_name] = CameraPanelSettings(
                camera_id=str(camera_id) if camera_id is not None else None,
                exposure_ms=float(table["exposure_ms"]),
                gain=int(table["gain"]),
                auto_exposure_enabled=bool(table.get("auto_exposure_enabled", False)),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return settings


def _toml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return str(value)


def _strip_table_blocks(lines: list[str], prefix: str) -> list[str]:
    """Drop every top-level table block whose header starts with `prefix`
    (e.g. ``"[cameras."``), keeping every other line untouched -- a
    line-scanned rewrite rather than a full TOML round-trip, same approach
    smart_telescope's ``api/location.py`` uses for its own multi-table
    ``config.toml`` (see that project's ``[locations.*]`` sections) so
    rewriting this module's own tables can never corrupt a sibling table
    (e.g. ``[mount_alignment]``, see ``mount_alignment_settings.py``)
    sharing the same file."""
    kept: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped.startswith(prefix)
        if not skipping:
            kept.append(line)
    return kept


def save_camera_settings(
    settings: dict[str, CameraPanelSettings],
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> None:
    """Write ``{panel_name: CameraPanelSettings}`` to `path` as TOML.

    Replaces only the ``[cameras.*]`` tables; any other top-level table
    already in the file (e.g. ``[mount_alignment]``) is preserved verbatim
    -- see `_strip_table_blocks`. A missing/unreadable file is treated the
    same as an empty one (nothing to preserve), matching `load_camera_settings`'s
    own tolerance.
    """
    target = Path(path)
    try:
        existing_lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        existing_lines = []

    kept = _strip_table_blocks(existing_lines, "[cameras.")
    while kept and kept[-1] == "":
        kept.pop()

    lines = list(kept)
    if lines:
        lines.append("")
    for panel_name in sorted(settings):
        panel = settings[panel_name]
        lines.append(f"[cameras.{panel_name}]")
        lines.append(f"camera_id = {_toml_scalar(panel.camera_id or '')}")
        lines.append(f"exposure_ms = {_toml_scalar(panel.exposure_ms)}")
        lines.append(f"gain = {_toml_scalar(panel.gain)}")
        lines.append(f"auto_exposure_enabled = {_toml_scalar(panel.auto_exposure_enabled)}")
        lines.append("")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
