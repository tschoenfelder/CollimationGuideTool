import pytest
from collimation_tool.ui.fov_overlay import FovOverlayRect, compute_fov_overlay_rect

# The actual rig this feature was built for (see ~/.SmartTScope/config.toml):
#   main  = ATR585M on a C8 at f/10 (2032mm focal)      -> 0.38 arcsec/px
#   guide = GPCMOS02000KPA on a 50/180 guide scope       -> ~3.32 arcsec/px
# Both sensors happen to be 16:9, so the FOV ratio is a single scalar in
# both dimensions (~0.229 — the main camera sees about 23% of the guide
# camera's field of view, in width and height alike).
_MAIN_PIXEL_SCALE = 0.38
_MAIN_WIDTH_PX, _MAIN_HEIGHT_PX = 3840, 2160
_GUIDE_PIXEL_SCALE = 3.32
_GUIDE_WIDTH_PX, _GUIDE_HEIGHT_PX = 1920, 1080


def _real_rig_rect() -> FovOverlayRect | None:
    return compute_fov_overlay_rect(
        main_pixel_scale_arcsec=_MAIN_PIXEL_SCALE,
        main_sensor_width_px=_MAIN_WIDTH_PX,
        main_sensor_height_px=_MAIN_HEIGHT_PX,
        guide_pixel_scale_arcsec=_GUIDE_PIXEL_SCALE,
        guide_sensor_width_px=_GUIDE_WIDTH_PX,
        guide_sensor_height_px=_GUIDE_HEIGHT_PX,
    )


class TestRealRigGeometry:
    """Verified against the actual ATR585M (main) / GPCMOS02000KPA (guide)
    rig this feature was requested for."""

    def test_main_fov_is_about_23_percent_of_the_guide_fov(self) -> None:
        rect = _real_rig_rect()
        assert rect is not None
        assert rect.width == pytest.approx(0.2289, abs=0.001)
        assert rect.height == pytest.approx(0.2289, abs=0.001)

    def test_width_and_height_fractions_match_since_both_sensors_are_16_9(
        self,
    ) -> None:
        rect = _real_rig_rect()
        assert rect is not None
        assert rect.width == pytest.approx(rect.height, rel=1e-9)

    def test_rectangle_is_centered_pending_real_alignment_calibration(self) -> None:
        rect = _real_rig_rect()
        assert rect is not None
        assert rect.x == pytest.approx((1.0 - rect.width) / 2.0)
        assert rect.y == pytest.approx((1.0 - rect.height) / 2.0)
        # Centered in a symmetric sense: equal margin on both sides.
        assert rect.x == pytest.approx(1.0 - rect.x - rect.width)
        assert rect.y == pytest.approx(1.0 - rect.y - rect.height)

    def test_rectangle_is_fully_inside_the_unit_square(self) -> None:
        rect = _real_rig_rect()
        assert rect is not None
        assert rect.x >= 0.0
        assert rect.y >= 0.0
        assert rect.x + rect.width <= 1.0
        assert rect.y + rect.height <= 1.0


class TestFallbacks:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"main_pixel_scale_arcsec": 0.0},
            {"guide_pixel_scale_arcsec": 0.0},
            {"main_sensor_width_px": 0},
            {"main_sensor_height_px": 0},
            {"guide_sensor_width_px": 0},
            {"guide_sensor_height_px": 0},
            {"main_pixel_scale_arcsec": -1.0},
        ],
    )
    def test_any_non_positive_input_yields_no_overlay(
        self, kwargs: dict[str, float | int]
    ) -> None:
        base: dict[str, float | int] = {
            "main_pixel_scale_arcsec": _MAIN_PIXEL_SCALE,
            "main_sensor_width_px": _MAIN_WIDTH_PX,
            "main_sensor_height_px": _MAIN_HEIGHT_PX,
            "guide_pixel_scale_arcsec": _GUIDE_PIXEL_SCALE,
            "guide_sensor_width_px": _GUIDE_WIDTH_PX,
            "guide_sensor_height_px": _GUIDE_HEIGHT_PX,
        }
        base.update(kwargs)
        assert compute_fov_overlay_rect(**base) is None  # type: ignore[arg-type]

    def test_a_main_fov_larger_than_the_guide_fov_is_clipped_to_fill_the_frame(
        self,
    ) -> None:
        # An unusual/misconfigured setup: main "sees more sky" than guide.
        rect = compute_fov_overlay_rect(
            main_pixel_scale_arcsec=5.0,
            main_sensor_width_px=1000,
            main_sensor_height_px=1000,
            guide_pixel_scale_arcsec=0.5,
            guide_sensor_width_px=1000,
            guide_sensor_height_px=1000,
        )
        assert rect is not None
        assert rect.width == 1.0
        assert rect.height == 1.0
        assert rect.x == 0.0
        assert rect.y == 0.0

    def test_equal_fov_on_both_cameras_fills_the_whole_guide_frame(self) -> None:
        rect = compute_fov_overlay_rect(
            main_pixel_scale_arcsec=1.0,
            main_sensor_width_px=100,
            main_sensor_height_px=100,
            guide_pixel_scale_arcsec=1.0,
            guide_sensor_width_px=100,
            guide_sensor_height_px=100,
        )
        assert rect is not None
        assert rect.x == pytest.approx(0.0)
        assert rect.y == pytest.approx(0.0)
        assert rect.width == pytest.approx(1.0)
        assert rect.height == pytest.approx(1.0)
