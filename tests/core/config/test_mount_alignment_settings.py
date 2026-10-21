import logging
from pathlib import Path

import pytest
from astrotool_core.config import mount_alignment_settings as settings_module
from astrotool_core.config.mount_alignment_settings import (
    MountAlignmentSettings,
    load_mount_alignment_settings,
)
from astrotool_core.mount.movement_sizing import SIDEREAL_ARCSEC_PER_S


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    assert load_mount_alignment_settings(tmp_path / "does-not-exist.toml") == (
        MountAlignmentSettings()
    )


def test_malformed_toml_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("this is not [valid toml", encoding="utf-8")
    assert load_mount_alignment_settings(path) == MountAlignmentSettings()


def test_file_without_a_mount_alignment_table_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[cameras.main]\ncamera_id = "dev-1"\n', encoding="utf-8")
    assert load_mount_alignment_settings(path) == MountAlignmentSettings()


def test_reads_a_full_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[mount_alignment]\n"
        "nudge_target_fraction = 0.25\nsettle_ms = 800\nframe_settle_ms = 300\n"
        "max_nudge_arcsec = 1500.0\nstability_tolerance_px = 4.5\n"
        "stability_sample_count = 5\nstability_sample_interval_s = 0.3\n"
        "stability_timeout_s = 12.0\nscreen_move_small_fraction = 0.04\n"
        "screen_move_medium_fraction = 0.12\nscreen_move_large_fraction = 0.25\n",
        encoding="utf-8",
    )
    assert load_mount_alignment_settings(path) == MountAlignmentSettings(
        nudge_target_fraction=0.25,
        settle_ms=800, frame_settle_ms=300, max_nudge_arcsec=1500.0,
        stability_tolerance_px=4.5, stability_sample_count=5,
        stability_sample_interval_s=0.3, stability_timeout_s=12.0,
        screen_move_small_fraction=0.04, screen_move_medium_fraction=0.12,
        screen_move_large_fraction=0.25,
    )


def test_missing_individual_values_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[mount_alignment]\nmin_calibration_pulse_ms = 300\n", encoding="utf-8")
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert settings.nudge_target_fraction == MountAlignmentSettings().nudge_target_fraction
    assert settings.settle_ms == MountAlignmentSettings().settle_ms
    assert settings.frame_settle_ms == MountAlignmentSettings().frame_settle_ms
    assert settings.max_nudge_arcsec == MountAlignmentSettings().max_nudge_arcsec
    assert settings.stability_tolerance_px == MountAlignmentSettings().stability_tolerance_px
    assert settings.stability_sample_count == MountAlignmentSettings().stability_sample_count
    assert (
        settings.stability_sample_interval_s
        == MountAlignmentSettings().stability_sample_interval_s
    )
    assert settings.stability_timeout_s == MountAlignmentSettings().stability_timeout_s
    assert (
        settings.screen_move_small_fraction
        == MountAlignmentSettings().screen_move_small_fraction
    )
    assert (
        settings.screen_move_medium_fraction
        == MountAlignmentSettings().screen_move_medium_fraction
    )
    assert (
        settings.screen_move_large_fraction
        == MountAlignmentSettings().screen_move_large_fraction
    )


def test_malformed_max_nudge_arcsec_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\nmax_nudge_arcsec = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert settings.max_nudge_arcsec == MountAlignmentSettings().max_nudge_arcsec


def test_malformed_settle_ms_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\nsettle_ms = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert settings.settle_ms == MountAlignmentSettings().settle_ms


def test_malformed_frame_settle_ms_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\nframe_settle_ms = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert settings.frame_settle_ms == MountAlignmentSettings().frame_settle_ms


def test_malformed_individual_value_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = "not a number"\n'
        'nudge_target_fraction = 0.1\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == MountAlignmentSettings().min_calibration_pulse_ms
    assert settings.nudge_target_fraction == 0.1


def test_malformed_nudge_target_fraction_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\n'
        'nudge_target_fraction = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert settings.nudge_target_fraction == MountAlignmentSettings().nudge_target_fraction


def test_malformed_stability_tolerance_px_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\n'
        'stability_tolerance_px = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert settings.stability_tolerance_px == MountAlignmentSettings().stability_tolerance_px


def test_malformed_stability_sample_count_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\n'
        'stability_sample_count = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert settings.stability_sample_count == MountAlignmentSettings().stability_sample_count


def test_malformed_stability_sample_interval_s_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\n'
        'stability_sample_interval_s = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert (
        settings.stability_sample_interval_s
        == MountAlignmentSettings().stability_sample_interval_s
    )


def test_malformed_stability_timeout_s_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\nstability_timeout_s = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert settings.stability_timeout_s == MountAlignmentSettings().stability_timeout_s


def test_malformed_screen_move_small_fraction_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\n'
        'screen_move_small_fraction = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert (
        settings.screen_move_small_fraction
        == MountAlignmentSettings().screen_move_small_fraction
    )


def test_malformed_screen_move_medium_fraction_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\n'
        'screen_move_medium_fraction = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert (
        settings.screen_move_medium_fraction
        == MountAlignmentSettings().screen_move_medium_fraction
    )


def test_malformed_screen_move_large_fraction_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\nmin_calibration_pulse_ms = 300\n'
        'screen_move_large_fraction = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.min_calibration_pulse_ms == 300
    assert (
        settings.screen_move_large_fraction
        == MountAlignmentSettings().screen_move_large_fraction
    )


def test_survives_a_sibling_cameras_table_in_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[cameras.main]\n"
        'camera_id = "dev-1"\n'
        "exposure_ms = 50.0\n"
        "gain = 120\n"
        "\n"
        "[mount_alignment]\n"
        "settle_ms = 500\n",
        encoding="utf-8",
    )
    assert load_mount_alignment_settings(path).settle_ms == 500


class TestCalibrationSizingSettings:
    """Issue #46: calibration move sizing is configurable but defaults to the
    finalized policy (25% target, 3 s cap, 0.2 s floor; S6.8d: no rate preset)."""

    def test_defaults_follow_the_issue_policy(self) -> None:
        settings = MountAlignmentSettings()

        assert settings.calibration_target_fraction == 0.25
        assert settings.max_calibration_pulse_ms == 3000
        assert settings.min_calibration_pulse_ms == 200

    def test_reads_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            "[mount_alignment]\n"
            "calibration_target_fraction = 0.3\nmax_calibration_pulse_ms = 2500\n"
            "min_calibration_pulse_ms = 150\n",
            encoding="utf-8",
        )

        settings = load_mount_alignment_settings(path)

        assert settings.calibration_target_fraction == 0.3
        assert settings.max_calibration_pulse_ms == 2500
        assert settings.min_calibration_pulse_ms == 150

    def test_malformed_values_fall_back_to_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            "[mount_alignment]\ncalibration_target_fraction = \"lots\"\n"
            "max_calibration_pulse_ms = \"long\"\n",
            encoding="utf-8",
        )

        settings = load_mount_alignment_settings(path)

        assert settings.calibration_target_fraction == 0.25
        assert settings.max_calibration_pulse_ms == 3000


class TestGuideRateSeed:
    """S6.8b: the ASSUMED guide rate that sizes the first guide-pulse move -- one named,
    configurable owner (OnStepAdapter 0.5.0 exposes no guide rate)."""

    def test_default_is_one_x_sidereal(self) -> None:
        assert MountAlignmentSettings().guide_rate_seed_x == 1.0

    def test_configured_value_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[mount_alignment]\nguide_rate_seed_x = 0.5\n", encoding="utf-8")
        assert load_mount_alignment_settings(path).guide_rate_seed_x == 0.5

    def test_a_non_positive_or_malformed_seed_falls_back_to_the_default(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        for value in ("0", "-1.0", '"fast"', "nan"):
            path.write_text(f"[mount_alignment]\nguide_rate_seed_x = {value}\n", encoding="utf-8")
            assert load_mount_alignment_settings(path).guide_rate_seed_x == 1.0, value


class TestRemovedTimedSettings:
    """S6.8d (R9): the timed-move keys are gone. The Pi's existing config.toml may still have
    them: it MUST keep loading, each removed key ignored with one warning naming it."""

    _OLD = (
        "[mount_alignment]\n"
        "pulse_ms = 750\n"
        'rate_preset = "5"\n'
        'calibration_rate_preset = "6"\n'
        "max_nudge_pulse_ms = 2500\n"
        "settle_ms = 800\n"
    )

    def test_an_old_config_keeps_loading_and_warns_once_per_removed_key(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text(self._OLD, encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=settings_module.__name__):
            settings = load_mount_alignment_settings(path)
        assert settings.settle_ms == 800  # the rest of the table still applies
        assert settings.max_nudge_arcsec == MountAlignmentSettings().max_nudge_arcsec
        for key in ("pulse_ms", "rate_preset", "calibration_rate_preset", "max_nudge_pulse_ms"):
            assert not hasattr(settings, key), key
            warnings = [r.getMessage() for r in caplog.records if f" {key} " in r.getMessage()]
            assert len(warnings) == 1, (key, warnings)
            assert "ignored" in warnings[0]

    def test_a_current_config_does_not_warn(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[mount_alignment]\nmax_nudge_arcsec = 900.0\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=settings_module.__name__):
            settings = load_mount_alignment_settings(path)
        assert settings.max_nudge_arcsec == 900.0
        assert caplog.records == []

    def test_a_malformed_nudge_cap_falls_back_to_the_default(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[mount_alignment]\nmax_nudge_arcsec = -3\n", encoding="utf-8")
        assert load_mount_alignment_settings(path).max_nudge_arcsec == (
            MountAlignmentSettings().max_nudge_arcsec
        )


def test_the_explicit_nudge_cap_default_equals_the_former_derived_cap() -> None:
    """F11 (S6.8d): behaviour-neutral -- the former cap was `max_nudge_pulse_ms` (3000 ms) at
    the nudge `rate_preset` "7" (48 x sidereal), computed exactly like this."""
    former = 3000 * (48.0 * SIDEREAL_ARCSEC_PER_S) / 1000.0
    assert MountAlignmentSettings().max_nudge_arcsec == former
