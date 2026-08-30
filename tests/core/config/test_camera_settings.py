from pathlib import Path

from astrotool_core.config.camera_settings import (
    CameraPanelSettings,
    load_camera_settings,
    save_camera_settings,
)


class TestLoadCameraSettings:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_camera_settings(tmp_path / "does-not-exist.toml") == {}

    def test_malformed_toml_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("this is not [valid toml", encoding="utf-8")
        assert load_camera_settings(path) == {}

    def test_file_without_a_cameras_table_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('[mount]\nname = "OnStep"\n', encoding="utf-8")
        assert load_camera_settings(path) == {}

    def test_reads_one_panel_table(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            "[cameras.main]\n"
            'camera_id = "tp-4-3-5-0547-157c"\n'
            "exposure_ms = 50.0\n"
            "gain = 120\n"
            "auto_exposure_enabled = false\n",
            encoding="utf-8",
        )
        settings = load_camera_settings(path)
        assert settings == {
            "main": CameraPanelSettings(
                camera_id="tp-4-3-5-0547-157c",
                exposure_ms=50.0,
                gain=120,
                auto_exposure_enabled=False,
            )
        }

    def test_empty_camera_id_reads_back_as_none(self, tmp_path: Path) -> None:
        """The demo camera — see save_camera_settings writing "" for None."""
        path = tmp_path / "config.toml"
        path.write_text(
            '[cameras.guide]\ncamera_id = ""\nexposure_ms = 20.0\ngain = 100\n',
            encoding="utf-8",
        )
        settings = load_camera_settings(path)
        assert settings["guide"].camera_id is None

    def test_a_malformed_panel_is_skipped_without_dropping_the_others(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            "[cameras.main]\n"
            'camera_id = "dev-1"\n'
            "exposure_ms = 10.0\n"
            "gain = 100\n"
            "\n"
            "[cameras.guide]\n"
            'camera_id = "dev-2"\n'
            "# exposure_ms missing entirely\n"
            "gain = 100\n",
            encoding="utf-8",
        )
        settings = load_camera_settings(path)
        assert set(settings) == {"main"}


class TestSaveCameraSettings:
    def test_round_trips_through_load(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        original = {
            "main": CameraPanelSettings(
                camera_id="dev-1", exposure_ms=50.0, gain=120, auto_exposure_enabled=False
            ),
            "guide": CameraPanelSettings(
                camera_id=None, exposure_ms=20.5, gain=3200, auto_exposure_enabled=True
            ),
        }
        save_camera_settings(original, path)
        assert load_camera_settings(path) == original

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "does" / "not" / "exist" / "config.toml"
        save_camera_settings(
            {"main": CameraPanelSettings(None, 20.0, 100, False)},
            path,
        )
        assert path.is_file()

    def test_overwrites_a_previous_file_entirely(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        save_camera_settings(
            {
                "main": CameraPanelSettings(None, 20.0, 100, False),
                "guide": CameraPanelSettings(None, 20.0, 100, False),
            },
            path,
        )
        save_camera_settings({"main": CameraPanelSettings(None, 30.0, 150, True)}, path)
        assert set(load_camera_settings(path)) == {"main"}

    def test_camera_id_with_special_characters_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        original = {
            "main": CameraPanelSettings(
                camera_id='weird"id\\here', exposure_ms=1.0, gain=100, auto_exposure_enabled=False
            )
        }
        save_camera_settings(original, path)
        assert load_camera_settings(path) == original
