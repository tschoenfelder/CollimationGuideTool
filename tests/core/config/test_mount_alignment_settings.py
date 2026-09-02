from pathlib import Path

from astrotool_core.config.mount_alignment_settings import (
    MountAlignmentSettings,
    load_mount_alignment_settings,
)


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
        "[mount_alignment]\npulse_ms = 750\nrate_preset = \"5\"\n"
        "nudge_target_fraction = 0.25\nsettle_ms = 800\nframe_settle_ms = 300\n"
        "max_nudge_pulse_ms = 2500\n",
        encoding="utf-8",
    )
    assert load_mount_alignment_settings(path) == MountAlignmentSettings(
        pulse_ms=750, rate_preset="5", nudge_target_fraction=0.25,
        settle_ms=800, frame_settle_ms=300, max_nudge_pulse_ms=2500,
    )


def test_missing_individual_values_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[mount_alignment]\npulse_ms = 2000\n", encoding="utf-8")
    settings = load_mount_alignment_settings(path)
    assert settings.pulse_ms == 2000
    assert settings.rate_preset == MountAlignmentSettings().rate_preset
    assert settings.nudge_target_fraction == MountAlignmentSettings().nudge_target_fraction
    assert settings.settle_ms == MountAlignmentSettings().settle_ms
    assert settings.frame_settle_ms == MountAlignmentSettings().frame_settle_ms
    assert settings.max_nudge_pulse_ms == MountAlignmentSettings().max_nudge_pulse_ms


def test_malformed_max_nudge_pulse_ms_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\npulse_ms = 200\nmax_nudge_pulse_ms = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.pulse_ms == 200
    assert settings.max_nudge_pulse_ms == MountAlignmentSettings().max_nudge_pulse_ms


def test_malformed_settle_ms_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\npulse_ms = 200\nsettle_ms = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.pulse_ms == 200
    assert settings.settle_ms == MountAlignmentSettings().settle_ms


def test_malformed_frame_settle_ms_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\npulse_ms = 200\nframe_settle_ms = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.pulse_ms == 200
    assert settings.frame_settle_ms == MountAlignmentSettings().frame_settle_ms


def test_malformed_individual_value_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\npulse_ms = "not a number"\nnudge_target_fraction = 0.1\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.pulse_ms == MountAlignmentSettings().pulse_ms
    assert settings.nudge_target_fraction == 0.1


def test_malformed_nudge_target_fraction_falls_back_to_default_without_dropping_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[mount_alignment]\npulse_ms = 200\nnudge_target_fraction = "not a number"\n',
        encoding="utf-8",
    )
    settings = load_mount_alignment_settings(path)
    assert settings.pulse_ms == 200
    assert settings.nudge_target_fraction == MountAlignmentSettings().nudge_target_fraction


def test_survives_a_sibling_cameras_table_in_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[cameras.main]\n"
        'camera_id = "dev-1"\n'
        "exposure_ms = 50.0\n"
        "gain = 120\n"
        "\n"
        "[mount_alignment]\n"
        "pulse_ms = 500\n",
        encoding="utf-8",
    )
    assert load_mount_alignment_settings(path).pulse_ms == 500
