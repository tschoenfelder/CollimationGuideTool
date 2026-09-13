"""Tests for Stage 4 radial diffraction profile — issue #18 AC 4.1-4.3:
compute a radial intensity profile around the registered star center,
detect the first diffraction ring where resolvable, and report an
explicit insufficient-sampling state rather than a misleading result."""

from __future__ import annotations

import pytest
from astrotool_core.diffraction.radial_profile import compute_radial_profile
from astrotool_core.target.frame_registration import register_frames
from astrotool_core.target.stacking import RollingFrameBuffer
from astrotool_core.testing.frame_factory import airy_pattern_image, star_field_image

_SHAPE = (120, 120)


class TestAC4_1SymmetricPsfProfile:
    def test_profile_is_independent_of_star_position_within_the_frame(self) -> None:
        image_a = airy_pattern_image(
            _SHAPE, x=60.0, y=50.0, peak=5000.0, core_sigma=3.0, background=100.0
        )
        image_b = airy_pattern_image(
            _SHAPE, x=90.0, y=65.0, peak=5000.0, core_sigma=3.0, background=100.0
        )

        result_a = compute_radial_profile(image_a)
        result_b = compute_radial_profile(image_b)

        assert result_a.sufficient_sampling is True
        assert result_b.sufficient_sampling is True
        assert result_a.profile is not None
        assert result_b.profile is not None
        # A rotationally symmetric pattern's own profile depends only on
        # radial distance, never on where in the frame the star sits --
        # trimming the last few bins avoids the part of the curve most
        # sensitive to exactly how far each center is from its own
        # nearest frame edge.
        n = min(len(result_a.profile.radii_px), len(result_b.profile.radii_px))
        compare_n = max(1, n - 3)
        for i in range(compare_n):
            assert result_a.profile.normalized_intensity[i] == pytest.approx(
                result_b.profile.normalized_intensity[i], abs=0.05
            )


class TestAC4_2RingDetection:
    def test_first_ring_position_is_detected_within_tolerance(self) -> None:
        ring_radius = 12.0
        image = airy_pattern_image(
            _SHAPE,
            x=60.0,
            y=60.0,
            peak=5000.0,
            core_sigma=2.0,
            background=100.0,
            ring_radius_px=ring_radius,
            ring_peak=1200.0,
            ring_sigma=1.5,
        )

        result = compute_radial_profile(image)

        assert result.sufficient_sampling is True
        assert result.first_ring_radius_px is not None
        assert result.first_ring_radius_px == pytest.approx(ring_radius, abs=2.0)

    def test_no_ring_present_reports_none_without_error(self) -> None:
        image = airy_pattern_image(
            _SHAPE, x=60.0, y=60.0, peak=5000.0, core_sigma=2.5, background=100.0
        )

        result = compute_radial_profile(image)

        assert result.sufficient_sampling is True
        assert result.first_ring_radius_px is None


class TestAC4_3InsufficientSampling:
    def test_heavily_undersampled_star_reports_insufficient_sampling(self) -> None:
        image = airy_pattern_image(
            (60, 80), x=30.0, y=25.0, peak=3000.0, core_sigma=1.0, background=100.0
        )

        result = compute_radial_profile(image, min_fwhm_px=3.0)

        assert result.sufficient_sampling is False
        assert result.reason == "insufficient_sampling"
        assert result.profile is None

    def test_starless_frame_reports_no_star_detected(self) -> None:
        image = star_field_image((60, 80), [], background=50.0)

        result = compute_radial_profile(image)

        assert result.sufficient_sampling is False
        assert result.reason == "no_star_detected"
        assert result.profile is None


class TestStage3ToStage4Pipeline:
    def test_stacked_frames_feed_directly_into_radial_profile(self) -> None:
        offsets = [(0.0, 0.0), (0.3, -0.2), (-0.2, 0.4)]
        frames = [
            airy_pattern_image(
                _SHAPE, x=60.0 + dx, y=60.0 + dy, peak=5000.0, core_sigma=2.5, background=100.0
            )
            for dx, dy in offsets
        ]

        registration = register_frames(frames)
        buffer = RollingFrameBuffer(capacity=10)
        for outcome in registration.outcomes:
            if outcome.usable and outcome.pixels is not None and outcome.star is not None:
                buffer.add(outcome.pixels, outcome.star)
        stack_result = buffer.produce_stack()
        assert stack_result.stacked is not None

        profile_result = compute_radial_profile(stack_result.stacked)

        assert profile_result.sufficient_sampling is True
        assert profile_result.center is not None
