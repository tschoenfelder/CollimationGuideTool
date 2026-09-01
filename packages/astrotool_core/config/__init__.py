"""Persisted, hand-rolled TOML config at ``~/.CollimationGuideTool/config.toml``
(see install.md's "Configuration" section) — per-camera-panel UI settings
and mount-alignment tuning share this one file; each module only ever
rewrites its own table(s), see camera_settings.py's docstring.
"""

from astrotool_core.config.camera_settings import (
    DEFAULT_CONFIG_PATH,
    CameraPanelSettings,
    load_camera_settings,
    save_camera_settings,
)
from astrotool_core.config.mount_alignment_settings import (
    MountAlignmentSettings,
    load_mount_alignment_settings,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "CameraPanelSettings",
    "load_camera_settings",
    "save_camera_settings",
    "MountAlignmentSettings",
    "load_mount_alignment_settings",
]
