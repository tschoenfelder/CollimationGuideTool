"""Tests for the star-mode autofocus analyzer — issue #33's own "Mode A"
test list: known best-focus sequence, monotonic worsening off-focus,
saturated-star rejection, multi-star robust aggregation, single-usable-star
reduced confidence, edge-clipped rejection."""

from __future__ import annotations

from astrotool_core.focus.star_focus_metric import measure_star_focus
from astrotool_core.testing.frame_factory import StarSpec, single_star_image, star_field_image

_SHAPE = (120, 120)


class TestMeasureStarFocus:
    def test_a_single_unsaturated_star_produces_a_usable_measurement(self) -> None:
        image = single_star_image(_SHAPE, x=60.0, y=60.0, peak=3000.0, sigma=2.5, background=100.0)

        measurement = measure_star_focus(image)

        assert measurement.fwhm_px is not None
        assert measurement.fwhm_px > 0.0
        assert measurement.usable_star_count == 1
        assert measurement.rejected_saturated == 0
        assert measurement.rejected_donut == 0
        assert measurement.rejected_edge == 0

    def test_a_smaller_sigma_reports_a_smaller_fwhm_than_a_larger_sigma(self) -> None:
        # Larger sigma == more defocus/blur -- FWHM should track it upward,
        # the monotonic relationship issue #33 requires near the optimum.
        sharp = single_star_image(_SHAPE, x=60.0, y=60.0, peak=3000.0, sigma=1.5, background=100.0)
        blurry = single_star_image(_SHAPE, x=60.0, y=60.0, peak=3000.0, sigma=4.0, background=100.0)

        sharp_measurement = measure_star_focus(sharp)
        blurry_measurement = measure_star_focus(blurry)

        assert sharp_measurement.fwhm_px is not None
        assert blurry_measurement.fwhm_px is not None
        assert sharp_measurement.fwhm_px < blurry_measurement.fwhm_px

    def test_a_saturated_star_is_rejected_not_averaged_in(self) -> None:
        # Peak far past the library's own 0.98-of-full-scale saturation
        # threshold (see smarttscope_live_analysis.analysis._detect_sources_
        # by_components) -- confirmed empirically this session.
        image = single_star_image(_SHAPE, x=60.0, y=60.0, peak=70000.0, sigma=2.5, background=100.0)

        measurement = measure_star_focus(image)

        assert measurement.fwhm_px is None
        assert measurement.usable_star_count == 0
        assert measurement.rejected_saturated == 1
        assert measurement.confidence == 0.0

    def test_an_edge_clipped_star_is_rejected(self) -> None:
        # Well within the default 16px margin of the frame's own left edge.
        image = single_star_image(_SHAPE, x=5.0, y=60.0, peak=3000.0, sigma=2.5, background=100.0)

        measurement = measure_star_focus(image, edge_margin_px=16)

        assert measurement.rejected_edge == 1
        assert measurement.usable_star_count == 0
        assert measurement.fwhm_px is None

    def test_multiple_usable_stars_use_a_robust_median_aggregate(self) -> None:
        stars = [
            StarSpec(x=30.0, y=30.0, peak=3000.0, sigma=2.0),
            StarSpec(x=60.0, y=60.0, peak=3000.0, sigma=2.5),
            StarSpec(x=90.0, y=90.0, peak=3000.0, sigma=3.0),
        ]
        image = star_field_image(_SHAPE, stars, background=100.0)

        measurement = measure_star_focus(image)

        assert measurement.usable_star_count == 3
        assert measurement.fwhm_px is not None
        assert measurement.confidence == 1.0  # >= _FULL_CONFIDENCE_STAR_COUNT

    def test_a_single_usable_star_still_focuses_with_reduced_confidence(self) -> None:
        image = single_star_image(_SHAPE, x=60.0, y=60.0, peak=3000.0, sigma=2.5, background=100.0)

        measurement = measure_star_focus(image)

        assert measurement.usable_star_count == 1
        assert measurement.fwhm_px is not None  # still usable
        assert 0.0 < measurement.confidence < 1.0  # reduced, not full

    def test_no_stars_at_all_reports_no_usable_measurement(self) -> None:
        image = star_field_image(_SHAPE, [], background=50.0)

        measurement = measure_star_focus(image)

        assert measurement.fwhm_px is None
        assert measurement.usable_star_count == 0
        assert measurement.confidence == 0.0

    def test_one_saturated_star_does_not_block_an_otherwise_usable_star(self) -> None:
        stars = [
            StarSpec(x=30.0, y=30.0, peak=3000.0, sigma=2.5),
            StarSpec(x=90.0, y=90.0, peak=70000.0, sigma=2.5),
        ]
        image = star_field_image(_SHAPE, stars, background=100.0)

        measurement = measure_star_focus(image)

        assert measurement.usable_star_count == 1
        assert measurement.fwhm_px is not None
        assert measurement.rejected_saturated == 1
