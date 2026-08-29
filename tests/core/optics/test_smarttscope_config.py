from pathlib import Path

import pytest
from astrotool_core.optics.smarttscope_config import (
    KNOWN_PIXEL_SIZE_UM,
    load_pixel_scale_arcsec,
)

# A trimmed copy of the relevant sections of the real rig's
# ~/.SmartTScope/config.toml (main = ATR585M on a C8 at f/10, guide =
# GPCMOS02000KPA on a 50/180 guide scope) — see the module docstring for
# why CollimationGuideTool reads this file instead of its own config.
_REAL_RIG_CONFIG = """
[session]
pixel_scale_arcsec = 0.38

[cameras.main]
model = "ATR585M"

[cameras.guide]
model = "GPCMOS02000KPA"

[telescopes.c8]
aperture_mm = 203.2
focal_mm = 2032.0
type = "sct"

[telescopes.guide_scope]
aperture_mm = 50.0
focal_mm = 180.0
type = "refractor"

[optical_trains.main]
telescope = "c8"
camera = "main"
reducer_factor = 1.0

[optical_trains.guide]
telescope = "guide_scope"
camera = "guide"
reducer_factor = 1.0
"""


@pytest.fixture
def rig_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_REAL_RIG_CONFIG, encoding="utf-8")
    return path


class TestRealRigConfig:
    """Verified against the actual rig this feature was built for."""

    def test_main_train_uses_the_session_level_override(self, rig_config: Path) -> None:
        # [session] pixel_scale_arcsec = 0.38 is explicitly documented in
        # the real config as "C8 native = 0.38" — the main train's own
        # pixel_scale_arcsec line is commented out there, so this is the
        # value that must be picked up.
        assert load_pixel_scale_arcsec("main", config_path=rig_config) == pytest.approx(0.38)

    def test_guide_train_is_computed_from_focal_length_and_known_pixel_size(
        self, rig_config: Path
    ) -> None:
        # 206264.8 * 0.0029mm / 180mm ≈ 3.323 arcsec/px — matches the real
        # config's own commented note ("guide scope 50/180 + 2.9 µm
        # sensor" -> "pixel_scale_arcsec = 3.32").
        result = load_pixel_scale_arcsec("guide", config_path=rig_config)
        assert result == pytest.approx(3.32, abs=0.01)

    def test_known_pixel_size_table_has_the_guide_camera(self) -> None:
        assert KNOWN_PIXEL_SIZE_UM["GPCMOS02000KPA"] == 2.9


class TestFallbacksAndMissingData:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert load_pixel_scale_arcsec("main", config_path=tmp_path / "nope.toml") is None

    def test_unknown_train_returns_none(self, rig_config: Path) -> None:
        assert load_pixel_scale_arcsec("oag", config_path=rig_config) is None

    def test_train_with_no_telescope_binding_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[optical_trains.main]\ncamera = \"main\"\n", encoding="utf-8")
        assert load_pixel_scale_arcsec("main", config_path=path) is None

    def test_unrecognized_camera_model_returns_none_rather_than_guessing(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            """
[cameras.guide]
model = "SomeFutureCameraNotInTheTable"

[telescopes.guide_scope]
focal_mm = 180.0

[optical_trains.guide]
telescope = "guide_scope"
camera = "guide"
""",
            encoding="utf-8",
        )
        assert load_pixel_scale_arcsec("guide", config_path=path) is None

    def test_explicit_train_level_override_wins_over_everything_else(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            """
[telescopes.guide_scope]
focal_mm = 180.0

[optical_trains.guide]
telescope = "guide_scope"
camera = "guide"
pixel_scale_arcsec = 9.99
""",
            encoding="utf-8",
        )
        assert load_pixel_scale_arcsec("guide", config_path=path) == pytest.approx(9.99)

    def test_reducer_factor_scales_the_effective_focal_length(self, tmp_path: Path) -> None:
        def _config(reducer_factor: float) -> Path:
            path = tmp_path / f"config_{reducer_factor}.toml"
            path.write_text(
                f"""
[cameras.main]
model = "GPCMOS02000KPA"

[telescopes.c8]
focal_mm = 2000.0

[optical_trains.main]
telescope = "c8"
camera = "main"
reducer_factor = {reducer_factor}
""",
                encoding="utf-8",
            )
            return path

        full = load_pixel_scale_arcsec("main", config_path=_config(1.0))
        reduced = load_pixel_scale_arcsec("main", config_path=_config(0.5))
        assert full is not None
        assert reduced is not None
        # Halving the effective focal length doubles the plate scale.
        assert reduced == pytest.approx(full * 2, rel=1e-6)

    def test_malformed_toml_returns_none(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("this is not [ valid toml", encoding="utf-8")
        assert load_pixel_scale_arcsec("main", config_path=path) is None
