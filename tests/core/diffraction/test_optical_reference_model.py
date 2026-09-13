"""Tests for Stage 5 optical diffraction reference model — issue #19
AC 5.1-5.2: calculate the expected Airy scale independently from the
measured profile, reporting an explicit degraded state (never a
fabricated default) when a required optical parameter is missing."""

from __future__ import annotations

import pytest
from astrotool_core.diffraction.optical_reference_model import (
    OpticalConfig,
    compute_diffraction_reference,
)
from astrotool_core.diffraction.radial_profile import compute_radial_profile
from astrotool_core.testing.frame_factory import airy_pattern_image


class TestAC5_1KnownAiryScale:
    def test_matches_a_hand_computed_reference_value_via_direct_focal_ratio(self) -> None:
        # Independently computed: 1.22 * (550nm = 0.55um) * f/10 / 5.0um
        # = 1.22 * 0.55 * 10 / 5.0 = 1.342 px.
        config = OpticalConfig(focal_ratio=10.0, wavelength_nm=550.0, pixel_size_um=5.0)

        result = compute_diffraction_reference(config)

        assert result.available is True
        assert result.focal_ratio_used == pytest.approx(10.0)
        assert result.expected_first_null_radius_px == pytest.approx(1.342, abs=0.01)

    def test_the_same_physical_setup_derived_from_aperture_and_focal_length_matches(
        self,
    ) -> None:
        # 200mm aperture, 2000mm focal length -> f/10, same as the direct
        # test above -- the derivation path must land on the identical result.
        config = OpticalConfig(
            aperture_mm=200.0, focal_length_mm=2000.0, wavelength_nm=550.0, pixel_size_um=5.0
        )

        result = compute_diffraction_reference(config)

        assert result.available is True
        assert result.focal_ratio_used == pytest.approx(10.0)
        assert result.expected_first_null_radius_px == pytest.approx(1.342, abs=0.01)


class TestAC5_2MissingOpticalParameter:
    def test_missing_pixel_size_reports_unavailable(self) -> None:
        config = OpticalConfig(focal_ratio=10.0, wavelength_nm=550.0, pixel_size_um=None)

        result = compute_diffraction_reference(config)

        assert result.available is False
        assert result.reason == "pixel_size_unavailable"
        assert result.expected_first_null_radius_px is None
        assert result.focal_ratio_used is None

    def test_missing_focal_ratio_and_aperture_pair_reports_unavailable(self) -> None:
        config = OpticalConfig(wavelength_nm=550.0, pixel_size_um=5.0)

        result = compute_diffraction_reference(config)

        assert result.available is False
        assert result.reason == "focal_ratio_unavailable"
        assert result.expected_first_null_radius_px is None

    def test_missing_wavelength_reports_unavailable(self) -> None:
        config = OpticalConfig(focal_ratio=10.0, pixel_size_um=5.0, wavelength_nm=None)

        result = compute_diffraction_reference(config)

        assert result.available is False
        assert result.reason == "wavelength_unavailable"
        assert result.expected_first_null_radius_px is None
        # focal_ratio was resolvable before the wavelength check failed.
        assert result.focal_ratio_used == pytest.approx(10.0)

    def test_no_fabricated_value_when_nothing_is_configured(self) -> None:
        result = compute_diffraction_reference(OpticalConfig())

        assert result.available is False
        assert result.expected_first_null_radius_px is None


class TestSyntheticTestGeneration:
    def test_expected_radius_feeds_directly_into_a_synthetic_frame_and_is_redetected(
        self,
    ) -> None:
        # Doc's own "The model shall also be usable for synthetic test
        # generation" -- demonstrated end to end, not just asserted.
        config = OpticalConfig(focal_ratio=6.0, wavelength_nm=550.0, pixel_size_um=2.5)
        reference = compute_diffraction_reference(config)
        assert reference.available is True
        assert reference.expected_first_null_radius_px is not None
        # Scaled up to a resolvable pixel radius for this synthetic frame.
        ring_radius = reference.expected_first_null_radius_px * 4

        image = airy_pattern_image(
            (120, 120),
            x=60.0,
            y=60.0,
            peak=5000.0,
            core_sigma=2.0,
            background=100.0,
            ring_radius_px=ring_radius,
            ring_peak=1200.0,
            ring_sigma=1.5,
        )

        profile_result = compute_radial_profile(image)

        assert profile_result.sufficient_sampling is True
        assert profile_result.first_ring_radius_px is not None
        assert profile_result.first_ring_radius_px == pytest.approx(ring_radius, abs=2.0)
