"""Persisted, hand-rolled TOML config at ``~/.CollimationGuideTool/config.toml``
(see install.md's "Configuration" section) — currently just per-camera-panel
UI settings; other tables (mount, session) can be added alongside without
disturbing this one, see camera_settings.py's docstring.
"""

from astrotool_core.config.camera_settings import (
    DEFAULT_CONFIG_PATH,
    CameraPanelSettings,
    load_camera_settings,
    save_camera_settings,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "CameraPanelSettings",
    "load_camera_settings",
    "save_camera_settings",
]
