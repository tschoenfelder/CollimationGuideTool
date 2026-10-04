"""#55 D03: the collimation app's config load observes the ONE config-path owner.

Only `astrotool_core.config.paths` is redirected (no consumer is patched on its
own); MainWindow's camera-settings/mount-alignment file, its plate-scale lookup
and main.py's filter-wheel wiring must all see the injected files."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.config import paths
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from collimation_tool.ui.main_window import MainWindow

_OWN = """\
[mount_alignment]
calibration_center_rate_x = 16.0

[filter_wheel]
enabled = true
active_camera_role = "guide"
device = "Alternate EFW"
"""

_SMARTTSCOPE = """\
[optical_trains.main]
pixel_scale_arcsec = 0.5

[optical_trains.guide]
pixel_scale_arcsec = 3.25
"""


@pytest.fixture
def alternate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    own = tmp_path / "alt-own.toml"
    shared = tmp_path / "alt-smarttscope.toml"
    own.write_text(_OWN)
    shared.write_text(_SMARTTSCOPE)
    monkeypatch.setattr(paths, "OWN_CONFIG_PATH", own)
    monkeypatch.setattr(paths, "SMARTTSCOPE_CONFIG_PATH", shared)
    return own


class TestMainWindowReadsTheOneOwner:
    def test_settings_file_mount_alignment_and_plate_scales(
        self, qapp: object, alternate_config: Path
    ) -> None:
        image = np.full((60, 80), 100.0, dtype=np.float32)
        window = MainWindow(
            ReplayCamera.from_arrays([image], cycle=True), device_lister=lambda: []
        )
        assert window._camera_settings_path == alternate_config
        assert window._mount_alignment_settings.calibration_center_rate_x == 16.0
        assert window._main_pixel_scale_arcsec == 0.5
        assert window._guide_pixel_scale_arcsec == 3.25

    def test_main_wires_the_filter_wheel_from_the_same_files(
        self, alternate_config: Path
    ) -> None:
        from collimation_tool.main import _default_filter_wheels

        (assignment,) = _default_filter_wheels()
        assert assignment.device_name == "Alternate EFW"
        assert assignment.trains == ("guide",)
        assert isinstance(assignment.port, IndiFilterWheelAdapter)
