"""#55 D01-D03: single-source device defaults and config locations.

One indiserver, one default endpoint: the filter wheel (registry and adapter),
the generic INDI client and OnStepAdapter's runtime config all talk to the same
local indiserver, so with nothing configured they must resolve the same host
and port. One owner for the config file locations: redirecting it redirects
every reader."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from astrotool_core.config import (
    device_defaults,
    load_camera_settings,
    load_mount_alignment_settings,
    paths,
    save_camera_settings,
)
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.filter_wheel.registry import (
    FilterWheelConfig,
    build_filter_wheels,
    load_filter_wheel_layout,
)
from astrotool_core.indi.client import IndiClient
from astrotool_core.onstep.settings import load_onstep_indi_config
from astrotool_core.optics import load_pixel_scale_arcsec
from astrotool_core.testing.fake_indi_server import FakeIndiServer


def _defaults(callable_: object) -> dict[str, object]:
    return {
        name: p.default
        for name, p in inspect.signature(callable_).parameters.items()  # type: ignore[arg-type]
        if p.default is not inspect.Parameter.empty
    }


class TestOneDefaultIndiEndpoint:
    def test_filter_wheel_and_onstep_resolve_the_same_indiserver(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.toml"
        wheel = load_filter_wheel_layout(smarttscope_path=missing, local_path=missing).wheels[0]
        onstep = load_onstep_indi_config(missing, missing)
        assert (wheel.host, wheel.port) == (onstep.host, onstep.port)

    def test_every_signature_default_names_that_same_endpoint(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.toml"
        onstep = load_onstep_indi_config(missing, missing)
        adapter = _defaults(IndiFilterWheelAdapter.__init__)
        wheel_config = _defaults(FilterWheelConfig)
        assert (adapter["host"], adapter["port"]) == (onstep.host, onstep.port)
        assert (wheel_config["host"], wheel_config["port"]) == (onstep.host, onstep.port)
        assert _defaults(IndiClient.__init__)["port"] == onstep.port


_OWN_CONFIG = """\
[cameras.Main]
exposure_ms = 123.0
gain = 7

[mount_alignment]
calibration_center_rate_x = 16.0

[indi]
host = "10.1.2.3"
port = 7700

[filter_wheel]
enabled = true
active_camera_role = "guide"
device = "Alternate EFW"
host = "10.1.2.3"
port = 7700
"""

_SMARTTSCOPE_CONFIG = """\
[observer]
lat = 47.5

[optical_trains.main]
pixel_scale_arcsec = 0.5
"""


@pytest.fixture
def alternate_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Redirect ONLY the single owner (`astrotool_core.config.paths`) -- no
    consumer is patched individually."""
    own = tmp_path / "alt-own.toml"
    shared = tmp_path / "alt-smarttscope.toml"
    own.write_text(_OWN_CONFIG)
    shared.write_text(_SMARTTSCOPE_CONFIG)
    monkeypatch.setattr(paths, "OWN_CONFIG_PATH", own)
    monkeypatch.setattr(paths, "SMARTTSCOPE_CONFIG_PATH", shared)
    return own, shared


class TestOneOwnerRedirectsEveryConsumer:
    """#55 D03: injecting an alternate config through the one path owner is
    observed by every reader (MainWindow's load: tests/collimation/ui/
    test_main_window_config_source.py)."""

    def test_camera_and_mount_alignment_settings(self, alternate_config: object) -> None:
        assert load_camera_settings()["Main"].gain == 7
        assert load_mount_alignment_settings().calibration_center_rate_x == 16.0

    def test_save_writes_to_the_injected_file(
        self, alternate_config: tuple[Path, Path]
    ) -> None:
        settings = load_camera_settings()
        save_camera_settings(settings)
        assert "[mount_alignment]" in alternate_config[0].read_text()  # same file rewritten
        assert load_camera_settings()["Main"].gain == 7

    def test_onstep_indi_config(self, alternate_config: object) -> None:
        config = load_onstep_indi_config()
        assert (config.host, config.port, config.observer_lat) == ("10.1.2.3", 7700, 47.5)

    def test_plate_scale(self, alternate_config: object) -> None:
        assert load_pixel_scale_arcsec("main") == 0.5

    def test_filter_wheel_registry_and_adapter_construction(
        self, alternate_config: object
    ) -> None:
        layout = load_filter_wheel_layout()
        (wheel,) = layout.wheels
        assert (wheel.device_name, wheel.host, wheel.port) == ("Alternate EFW", "10.1.2.3", 7700)
        assert layout.trains == (("guide", wheel.id),)
        (assignment,) = build_filter_wheels(layout)  # the production default factory
        adapter = assignment.port
        assert isinstance(adapter, IndiFilterWheelAdapter)
        # No public accessor for the configured endpoint; the adapter's own
        # client is what a connect() would dial.
        assert adapter._device_name == "Alternate EFW"
        assert (adapter._client._host, adapter._client._port) == ("10.1.2.3", 7700)


class TestDiagnosticsDirectoryFollowsTheOwner:
    """#55 D03: the diagnostics directory is resolved at call time too, so
    one patch of the owner redirects every default bundle writer/reader."""

    def test_service_find_and_pull_use_the_injected_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from astrotool_core.diagnostics.remote import pull_bundle
        from astrotool_core.diagnostics.service import DiagnosticService, find_bundle

        injected = tmp_path / "alt-diagnostics"
        (injected / "abc12345-bundle").mkdir(parents=True)
        monkeypatch.setattr(paths, "DIAGNOSTICS_DIR", injected)

        service = DiagnosticService(app_name="T", version="0", git_commit="0")
        assert service._diagnostics_dir == injected
        assert find_bundle("abc12345") == injected / "abc12345-bundle"

        def no_remote(_args: object) -> object:
            raise AssertionError("an existing local bundle must not be fetched")

        assert pull_bundle("abc12345", runner=no_remote) == injected / "abc12345-bundle"  # type: ignore[arg-type]


class TestBuiltInDefaultsAreTheOwnersValues:
    """#55 D01/D02: with nothing configured, every consumer resolves the
    owner's value -- the registry, the adapter, the simulator, the client."""

    def test_device_and_endpoint(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing.toml"
        (wheel,) = load_filter_wheel_layout(smarttscope_path=missing, local_path=missing).wheels
        onstep = load_onstep_indi_config(missing, missing)
        adapter = _defaults(IndiFilterWheelAdapter.__init__)
        endpoint = (device_defaults.INDI_HOST, device_defaults.INDI_PORT)
        assert wheel.device_name == adapter["device_name"] == device_defaults.EFW_DEVICE_NAME
        assert _defaults(FakeIndiServer.__init__)["device_name"] == device_defaults.EFW_DEVICE_NAME
        assert (wheel.host, wheel.port) == (onstep.host, onstep.port) == endpoint
        assert (adapter["host"], adapter["port"]) == endpoint

    def test_compatibility_aliases_name_the_owners_locations(self) -> None:
        import astrotool_core.config as config_package
        import astrotool_core.optics as optics_package
        from astrotool_core.diagnostics.remote import DEFAULT_REMOTE_DIAGNOSTICS_DIR
        from astrotool_core.diagnostics.service import DEFAULT_DIAGNOSTICS_DIR

        own = Path.home() / paths.APP_DIR_NAME / paths.CONFIG_FILE_NAME
        shared = Path.home() / paths.SMARTTSCOPE_DIR_NAME / paths.CONFIG_FILE_NAME
        assert own == config_package.DEFAULT_CONFIG_PATH
        assert shared == optics_package.DEFAULT_CONFIG_PATH
        assert Path.home() / paths.APP_DIR_NAME / "diagnostics" == DEFAULT_DIAGNOSTICS_DIR
        # Every alias names the fixed real location, never a redirected one
        # (conftest has redirected the active locations for this very test).
        import astrotool_core.config.mount_alignment_settings as mount_alignment
        import astrotool_core.filter_wheel.config as filter_wheel_config
        import astrotool_core.onstep.settings as onstep_settings

        assert own == mount_alignment.DEFAULT_CONFIG_PATH == onstep_settings.DEFAULT_CONFIG_PATH
        assert own == filter_wheel_config.DEFAULT_LOCAL_CONFIG_PATH
        assert shared == onstep_settings.SMARTTSCOPE_CONFIG_PATH
        assert shared == filter_wheel_config.DEFAULT_SMARTTSCOPE_CONFIG_PATH
        assert paths.own_config_path() != own  # the active one IS redirected
        assert DEFAULT_REMOTE_DIAGNOSTICS_DIR == "~/.CollimationGuideTool/diagnostics"
