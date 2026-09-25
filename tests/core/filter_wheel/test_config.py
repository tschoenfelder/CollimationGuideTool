"""Issue #47: the filter wheel's wiring (which optical train it serves, its
per-slot filter names) is read from the shared SmartTScope config, not a
CollimationGuideTool-invented parallel schema -- see filter_wheel/config.py's
own docstring for why."""

from __future__ import annotations

from pathlib import Path

from astrotool_core.filter_wheel.config import (
    FilterWheelWiring,
    load_filter_wheel_wiring,
)

_SHARED_TABLE = (
    "[filter_wheel]\n"
    "enabled = true\n"
    'active_camera_role = "main"\n'
    "settle_s = 1.5\n"
    "\n"
    "[filters]\n"
    "luminance = 1\n"
    "red       = 2\n"
    "green     = 3\n"
    "blue      = 4\n"
    "ha        = 5\n"
    "oiii      = 6\n"
    "sii       = 7\n"
)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class TestSharedConfigWins:
    def test_reads_the_real_shape_of_the_shared_config(self, tmp_path: Path) -> None:
        shared = _write(tmp_path / "smarttscope.toml", _SHARED_TABLE)
        local = tmp_path / "local.toml"  # absent

        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=local)

        assert wiring == FilterWheelWiring(
            enabled=True,
            active_train="main",
            filter_names={1: "L", 2: "R", 3: "G", 4: "B", 5: "H", 6: "O", 7: "S"},
            device_name="ToupTek EFW 2",
            host=None,
            port=None,
        )

    def test_an_unknown_filters_key_is_skipped_not_guessed(self, tmp_path: Path) -> None:
        shared = _write(
            tmp_path / "smarttscope.toml",
            '[filter_wheel]\nenabled = true\nactive_camera_role = "main"\n'
            "[filters]\nluminance = 1\nsome_future_filter = 8\n",
        )
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=tmp_path / "x.toml")
        assert wiring.filter_names == {1: "L"}

    def test_local_indi_identity_is_layered_onto_the_shared_wiring(self, tmp_path: Path) -> None:
        shared = _write(tmp_path / "smarttscope.toml", _SHARED_TABLE)
        local = _write(
            tmp_path / "local.toml",
            '[filter_wheel]\ndevice = "ToupTek EFW 2"\nhost = "rasppi3"\nport = 7625\n',
        )
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=local)
        assert (wiring.host, wiring.port) == ("rasppi3", 7625)
        assert wiring.active_train == "main"  # still from the shared file


class TestSharedConfigExplicitlyDisabled:
    def test_disabled_in_the_shared_config_is_authoritative_no_fallback(
        self, tmp_path: Path
    ) -> None:
        shared = _write(
            tmp_path / "smarttscope.toml",
            '[filter_wheel]\nenabled = false\nactive_camera_role = "main"\n',
        )
        local = _write(
            tmp_path / "local.toml",
            '[filter_wheel]\nenabled = true\nactive_camera_role = "guide"\n',
        )
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=local)
        assert wiring == FilterWheelWiring(
            enabled=False,
            active_train=None,
            filter_names={},
            device_name=None,
            host=None,
            port=None,
        )

    def test_enabled_but_no_active_role_is_also_authoritative(self, tmp_path: Path) -> None:
        shared = _write(tmp_path / "smarttscope.toml", "[filter_wheel]\nenabled = true\n")
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=tmp_path / "x.toml")
        assert wiring.enabled is False
        assert wiring.active_train is None


class TestLocalFallback:
    def test_falls_back_to_the_same_shape_in_the_local_file(self, tmp_path: Path) -> None:
        local = _write(
            tmp_path / "local.toml",
            "[filter_wheel]\nenabled = true\n"
            'active_camera_role = "guide"\n'
            'device = "ToupTek EFW 2"\n'
            "[filters]\nred = 1\n",
        )
        wiring = load_filter_wheel_wiring(
            smarttscope_path=tmp_path / "missing.toml", local_path=local
        )
        assert wiring.active_train == "guide"
        assert wiring.filter_names == {1: "R"}
        assert wiring.device_name == "ToupTek EFW 2"

    def test_shared_file_present_but_malformed_falls_through_to_local(self, tmp_path: Path) -> None:
        shared = _write(tmp_path / "smarttscope.toml", "this is [not valid toml")
        local = _write(
            tmp_path / "local.toml",
            '[filter_wheel]\nenabled = true\nactive_camera_role = "guide"\n',
        )
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=local)
        assert wiring.active_train == "guide"

    def test_shared_file_present_with_no_filter_wheel_table_falls_through(
        self, tmp_path: Path
    ) -> None:
        shared = _write(tmp_path / "smarttscope.toml", "[observer]\nlat = 50.0\n")
        local = _write(
            tmp_path / "local.toml",
            '[filter_wheel]\nenabled = true\nactive_camera_role = "guide"\n',
        )
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=local)
        assert wiring.active_train == "guide"


class TestBuiltInDefault:
    def test_neither_file_present_uses_the_known_good_default(self, tmp_path: Path) -> None:
        wiring = load_filter_wheel_wiring(
            smarttscope_path=tmp_path / "a.toml", local_path=tmp_path / "b.toml"
        )
        assert wiring == FilterWheelWiring(
            enabled=True,
            active_train="main",
            filter_names={1: "L", 2: "R", 3: "G", 4: "B", 5: "S", 6: "H", 7: "O", 8: "NONE"},
            device_name="ToupTek EFW 2",
            host=None,
            port=None,
        )

    def test_both_files_malformed_uses_the_known_good_default(self, tmp_path: Path) -> None:
        shared = _write(tmp_path / "smarttscope.toml", "not [valid")
        local = _write(tmp_path / "local.toml", "also not ] valid")
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=local)
        assert wiring.active_train == "main"
        assert wiring.device_name == "ToupTek EFW 2"


class TestLocalNamesOverride:
    """Real report: the INDI driver's own slot names (and SmartTScope's
    shared [filters] order) did not match this rig's physical wheel."""

    _LOCAL = (
        "[filters]\nluminance = 1\nred = 2\ngreen = 3\nblue = 4\n"
        "sii = 5\nha = 6\noiii = 7\nnone = 8\n"
    )

    def test_local_filters_replace_the_shared_names_and_flag_an_override(
        self, tmp_path: Path
    ) -> None:
        shared = _write(tmp_path / "smarttscope.toml", _SHARED_TABLE)
        local = _write(tmp_path / "local.toml", self._LOCAL)
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=local)
        assert wiring.filter_names == {
            1: "L", 2: "R", 3: "G", 4: "B", 5: "S", 6: "H", 7: "O", 8: "NONE",
        }
        assert wiring.names_override is True
        assert wiring.active_train == "main"  # the wiring itself is still shared

    def test_no_local_filters_table_leaves_device_names_in_charge(self, tmp_path: Path) -> None:
        shared = _write(tmp_path / "smarttscope.toml", _SHARED_TABLE)
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=tmp_path / "x.toml")
        assert wiring.names_override is False

    def test_an_explicitly_disabled_wheel_is_never_resurrected(self, tmp_path: Path) -> None:
        shared = _write(tmp_path / "smarttscope.toml", "[filter_wheel]\nenabled = false\n")
        local = _write(tmp_path / "local.toml", self._LOCAL)
        wiring = load_filter_wheel_wiring(smarttscope_path=shared, local_path=local)
        assert wiring.enabled is False
