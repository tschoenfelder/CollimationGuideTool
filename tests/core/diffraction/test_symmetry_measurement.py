"""Tests for Stage 6 focused-star symmetry / coma measurement — issue
#20 AC 6.1-6.4: determine whether the focused diffraction pattern is
rotationally symmetric and derive a fine-collimation error direction,
with an explicit low-confidence/invalid state rather than a misleading
recommendation."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.diffraction.optical_reference_model import (
    DiffractionReferenceResult,
    OpticalConfig,
    compute_diffraction_reference,
)
from astrotool_core.diffraction.radial_profile import compute_radial_profile
from astrotool_core.diffraction.symmetry_measurement import (
    SymmetryStatus,
    compute_symmetry_measurement,
)
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.stacking import RollingFrameBuffer
from astrotool_core.testing.frame_factory import airy_pattern_image, coma_pattern_image

_SHAPE = (120, 120)
_CENTER_X, _CENTER_Y = 60.0, 60.0
_RING_RADIUS = 12.0

_NO_REFERENCE = DiffractionReferenceResult(
    expected_first_null_radius_px=None, focal_ratio_used=None,
    available=False, reason="pixel_size_unavailable",
)


def _symmetric_frame() -> np.ndarray:
    return airy_pattern_image(
        _SHAPE, x=_CENTER_X, y=_CENTER_Y, peak=5000.0, core_sigma=2.0, background=100.0,
        ring_radius_px=_RING_RADIUS, ring_peak=1200.0, ring_sigma=1.5,
    )


def _asymmetric_frame(direction_deg: float, *, strength: float = 0.6) -> np.ndarray:
    # A ring this strong relative to the core would read as a genuine
    # donut/defocus candidate to the shared detector (confirmed via a
    # standalone check) -- these parameters keep it classified as a
    # normal_star while still producing a clear directional signal.
    return coma_pattern_image(
        _SHAPE, x=_CENTER_X, y=_CENTER_Y, peak=6000.0, core_sigma=2.5, background=100.0,
        ring_radius_px=_RING_RADIUS, ring_peak=300.0, ring_sigma=1.5,
        asymmetry_direction_deg=direction_deg, asymmetry_strength=strength,
    )


class TestAC6_1FineCollimated:
    def test_a_rotationally_symmetric_pattern_is_classified_fine_collimated(self) -> None:
        profile = compute_radial_profile(_symmetric_frame())
        assert profile.sufficient_sampling is True

        result = compute_symmetry_measurement(_symmetric_frame(), profile, _NO_REFERENCE)

        assert result.status is SymmetryStatus.FINE_COLLIMATED
        assert result.asymmetry_magnitude is not None
        assert result.asymmetry_magnitude == pytest.approx(0.0, abs=0.05)


class TestAC6_2KnownAsymmetryDirection:
    @pytest.mark.parametrize("direction_deg", [0.0, 45.0, 135.0, 200.0, 300.0])
    def test_reported_direction_matches_the_imposed_direction(self, direction_deg: float) -> None:
        frame = _asymmetric_frame(direction_deg)
        profile = compute_radial_profile(frame)
        assert profile.sufficient_sampling is True

        result = compute_symmetry_measurement(frame, profile, _NO_REFERENCE)

        assert result.status is SymmetryStatus.ASYMMETRIC
        assert result.asymmetry_direction_deg is not None
        diff = abs((result.asymmetry_direction_deg - direction_deg + 180) % 360 - 180)
        assert diff < 20.0


class TestAC6_3NoiseResistance:
    def test_the_recovered_direction_is_statistically_consistent_across_stacked_repeats(
        self,
    ) -> None:
        imposed_direction = 60.0
        directions: list[float] = []

        for seed in range(5):
            rng = np.random.default_rng(seed)
            buffer = RollingFrameBuffer(capacity=10)
            for _ in range(4):
                noisy = _asymmetric_frame(imposed_direction) + rng.normal(
                    0.0, 15.0, size=_SHAPE
                )
                detection = detect_sources(noisy)
                star = select_target(detection)
                assert star is not None
                buffer.add(noisy, star)
            stack_result = buffer.produce_stack()
            assert stack_result.stacked is not None

            profile = compute_radial_profile(stack_result.stacked)
            assert profile.sufficient_sampling is True
            result = compute_symmetry_measurement(stack_result.stacked, profile, _NO_REFERENCE)
            assert result.asymmetry_direction_deg is not None
            directions.append(result.asymmetry_direction_deg)

        deviations = [abs((d - imposed_direction + 180) % 360 - 180) for d in directions]
        assert max(deviations) < 25.0


class TestAC6_4LowConfidence:
    def test_a_very_faint_ring_reports_low_confidence_and_no_recommendation(self) -> None:
        frame = airy_pattern_image(
            _SHAPE, x=_CENTER_X, y=_CENTER_Y, peak=5000.0, core_sigma=2.0, background=100.0,
            ring_radius_px=_RING_RADIUS, ring_peak=5.0, ring_sigma=1.5,
        )
        profile = compute_radial_profile(frame)
        assert profile.sufficient_sampling is True

        result = compute_symmetry_measurement(frame, profile, _NO_REFERENCE)

        assert result.status is SymmetryStatus.LOW_CONFIDENCE
        assert result.confidence < 0.3


class TestInvalidPropagation:
    def test_insufficient_upstream_sampling_propagates_as_invalid(self) -> None:
        frame = airy_pattern_image(
            (60, 80), x=30.0, y=25.0, peak=3000.0, core_sigma=1.0, background=100.0,
        )
        profile = compute_radial_profile(frame, min_fwhm_px=3.0)
        assert profile.sufficient_sampling is False

        result = compute_symmetry_measurement(frame, profile, _NO_REFERENCE)

        assert result.status is SymmetryStatus.INVALID
        assert result.asymmetry_magnitude is None
        assert result.asymmetry_direction_deg is None
        assert result.confidence == 0.0
        assert result.reason == profile.reason


class TestReferenceModelFallback:
    def test_uses_the_expected_reference_radius_when_no_ring_is_measured(self) -> None:
        # A plain, ring-less symmetric frame -- #18's own first_ring_radius_px
        # is None, so this must fall back to #19's expected radius rather
        # than failing outright.
        frame = airy_pattern_image(
            _SHAPE, x=_CENTER_X, y=_CENTER_Y, peak=5000.0, core_sigma=2.5, background=100.0,
        )
        profile = compute_radial_profile(frame)
        assert profile.sufficient_sampling is True
        assert profile.first_ring_radius_px is None

        reference = compute_diffraction_reference(
            OpticalConfig(focal_ratio=10.0, wavelength_nm=550.0, pixel_size_um=5.0)
        )
        assert reference.available is True

        result = compute_symmetry_measurement(frame, profile, reference)

        assert result.status in (SymmetryStatus.FINE_COLLIMATED, SymmetryStatus.LOW_CONFIDENCE)
