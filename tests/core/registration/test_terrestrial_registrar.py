"""Tests for TerrestrialRegistrar — ported from the original
collimation_tool.ui.fov_registration test suite (issue #29 moved the
algorithm into astrotool_core.registration, unchanged) plus new coverage
for the common-contract wrapping (status classification, ambiguity
detection) that wrapping added.

Synthetic starfields (a flat background plus several Gaussian "stars" at
random positions) stand in for real sky/textured frames: they have
genuine local structure (unlike a flat test pattern, which correlates
everywhere equally) without needing real captured FITS data.
"""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.registration.geometry import polygon_centroid
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)
from astrotool_core.registration.terrestrial_registrar import (
    TERRESTRIAL_ACCEPTABLE_ACCURACY_PX,
    TerrestrialRegistrar,
    _resize_bilinear,
    _rotate_bilinear,
    _sharpness_ratio,
)

_registrar = TerrestrialRegistrar()


def _starfield(height: int, width: int, *, n_stars: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((height, width), 100.0, dtype=np.float64)
    ys, xs = np.meshgrid(np.arange(height), np.arange(width), indexing="ij")
    for _ in range(n_stars):
        cx, cy = rng.uniform(0, width), rng.uniform(0, height)
        peak = rng.uniform(500.0, 3000.0)
        sigma = rng.uniform(1.5, 3.0)
        image += peak * np.exp(-(((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sigma**2)))
    return image


def _priors(approx_scale: float = 1.0) -> tuple[OpticalPrior, OpticalPrior]:
    """A trivial (a, b) OpticalPrior pair whose `scale_ratio` is exactly
    `approx_scale` -- sensor dimensions are irrelevant to the algorithm
    itself (only `frame_a`/`frame_b`'s own real array shapes matter), so
    a fixed placeholder size is fine here."""
    prior_a = OpticalPrior(name="a", sensor_width_px=10, sensor_height_px=10,
                            pixel_scale_arcsec=approx_scale)
    prior_b = OpticalPrior(name="b", sensor_width_px=10, sensor_height_px=10,
                            pixel_scale_arcsec=1.0)
    return prior_a, prior_b


def _center(result: CrossCameraRegistrationResult) -> tuple[float, float]:
    assert result.polygon_a_in_b is not None
    return polygon_centroid(result.polygon_a_in_b)


class TestTranslationOnly:
    def test_recovers_the_exact_crop_location(self) -> None:
        guide = _starfield(200, 200, n_stars=60, seed=1)
        main = guide[80:140, 60:130].copy()  # h=60, w=70, top-left (80, 60)
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=5.0, angle_range_deg=(-5, 5),
        )

        assert result.status is RegistrationStatus.OK_OVERLAP
        assert result.method is RegistrationMethod.TERRESTRIAL
        cx, cy = _center(result)
        assert cx == pytest.approx(60 + 70 / 2.0, abs=0.5)
        assert cy == pytest.approx(80 + 60 / 2.0, abs=0.5)
        assert result.rotation_deg == pytest.approx(0.0, abs=1e-6)
        assert result.scale == pytest.approx(1.0)
        assert result.confidence is not None and result.confidence > 0.99


class TestRotation:
    def test_recovers_a_known_rotation(self) -> None:
        guide = _starfield(200, 200, n_stars=60, seed=2)
        patch = guide[50:150, 40:170].copy()  # 100x130
        rotated = _rotate_bilinear(patch, angle_deg=25.0, fill_value=float(patch.mean()))
        cy0 = rotated.shape[0] // 2 - 25
        cx0 = rotated.shape[1] // 2 - 30
        main = rotated[cy0 : cy0 + 50, cx0 : cx0 + 60]
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=2.0, angle_range_deg=(-180, 180),
        )

        assert result.status is RegistrationStatus.OK_OVERLAP
        assert result.rotation_deg == pytest.approx(-25.0, abs=2.0)
        assert result.confidence is not None and result.confidence > 0.95

    def test_supports_arbitrary_rotation_across_the_full_360_degree_range(self) -> None:
        """Issue #29 #2: relative rotation isn't assumed to stay near a
        small fixed angle -- confirms a rotation well past +/-90 degrees
        (unlike the other rotation tests here) is still recovered."""
        guide = _starfield(200, 200, n_stars=60, seed=15)
        patch = guide[50:150, 40:170].copy()
        rotated = _rotate_bilinear(patch, angle_deg=150.0, fill_value=float(patch.mean()))
        cy0 = rotated.shape[0] // 2 - 25
        cx0 = rotated.shape[1] // 2 - 30
        main = rotated[cy0 : cy0 + 50, cx0 : cx0 + 60]
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=2.0, angle_range_deg=(-180, 180),
        )

        assert result.status is RegistrationStatus.OK_OVERLAP
        assert result.rotation_deg == pytest.approx(-150.0, abs=2.0)


class TestScale:
    def test_recovers_a_known_scale_ratio(self) -> None:
        guide = _starfield(220, 220, n_stars=80, seed=4)
        crop = guide[70:150, 60:180]  # h=80, w=120
        main = _resize_bilinear(crop, 27, 40)  # roughly scale 1/3
        prior_a, prior_b = _priors(3.0)

        result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_search_fraction=0.3, scale_steps=7, angle_step_deg=3.0,
            angle_range_deg=(-6, 6),
        )

        assert result.status is RegistrationStatus.OK_OVERLAP
        assert result.scale == pytest.approx(3.0, abs=0.3)
        cx, cy = _center(result)
        assert cx == pytest.approx(60 + 120 / 2.0, abs=2.0)
        assert cy == pytest.approx(70 + 80 / 2.0, abs=2.0)

    def test_scale_steps_one_trusts_the_optical_prior_ratio_exactly(self) -> None:
        guide = _starfield(150, 150, n_stars=40, seed=5)
        main = guide[40:90, 50:110].copy()
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=5.0, angle_range_deg=(-5, 5),
        )
        assert result.status is RegistrationStatus.OK_OVERLAP
        assert result.scale == 1.0  # no search performed -- the prior ratio, verbatim


class TestExposureInvariance:
    def test_a_brightness_and_contrast_difference_does_not_affect_the_match(self) -> None:
        guide = _starfield(200, 200, n_stars=60, seed=6)
        main = guide[80:140, 60:130].copy()
        dimmer_main = main * 0.15 + 40.0
        prior_a, prior_b = _priors(1.0)

        bright_result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=5.0, angle_range_deg=(-5, 5),
        )
        dim_result = _registrar.register(
            dimmer_main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=5.0, angle_range_deg=(-5, 5),
        )

        assert bright_result.status is RegistrationStatus.OK_OVERLAP
        assert dim_result.status is RegistrationStatus.OK_OVERLAP
        bright_cx, bright_cy = _center(bright_result)
        dim_cx, dim_cy = _center(dim_result)
        assert dim_cx == pytest.approx(bright_cx)
        assert dim_cy == pytest.approx(bright_cy)
        assert dim_result.confidence == pytest.approx(bright_result.confidence, abs=1e-6)


class TestNoValidRegistration:
    def test_unrelated_content_reports_no_valid_registration(self) -> None:
        guide = _starfield(200, 200, n_stars=60, seed=7)
        rng = np.random.default_rng(99)
        unrelated = rng.normal(500.0, 50.0, size=(60, 70))
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            unrelated, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=10.0, angle_range_deg=(-30, 30),
        )
        assert result.status is RegistrationStatus.NO_VALID_REGISTRATION
        assert not result.ok
        assert result.polygon_a_in_b is None

    def test_a_flat_template_reports_insufficient_structure(self) -> None:
        guide = _starfield(120, 120, n_stars=30, seed=8)
        flat = np.full((30, 30), 500.0)
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            flat, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=10.0, angle_range_deg=(-30, 30),
        )
        assert result.status is RegistrationStatus.INSUFFICIENT_STRUCTURE

    def test_a_featureless_region_of_a_real_scene_reports_insufficient_structure(self) -> None:
        rng = np.random.default_rng(11)
        guide = np.full((120, 200), 800.0)
        guide[70:, :] = 50.0
        guide += _starfield(120, 200, n_stars=25, seed=11) - 100.0
        guide[70:, :] = 50.0 + rng.normal(0.0, 0.5, size=guide[70:, :].shape)
        featureless_main = guide[80:110, 60:140].copy()
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            featureless_main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=15.0, angle_range_deg=(-30, 30),
        )
        assert result.status is RegistrationStatus.INSUFFICIENT_STRUCTURE

    def test_a_template_too_large_for_every_candidate_scale_is_insufficient_structure(
        self,
    ) -> None:
        guide = _starfield(50, 50, n_stars=10, seed=11)
        main = np.full((80, 80), 500.0)  # bigger than guide on both axes
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            main, guide, prior_a, prior_b, scale_steps=1, angle_step_deg=30.0,
        )
        assert result.status is RegistrationStatus.INSUFFICIENT_STRUCTURE


class TestAmbiguousMatch:
    """New coverage: issue #29 #5 ("repeated patterns create materially
    ambiguous candidates")."""

    def test_two_equally_good_distant_candidates_are_reported_as_ambiguous(self) -> None:
        # A repeating pattern (two identical star clusters far apart) --
        # the template genuinely matches both locations equally well.
        base = _starfield(40, 40, n_stars=8, seed=40)
        guide = np.full((200, 200), 100.0)
        guide[10:50, 10:50] = base
        guide[10:50, 140:180] = base  # exact repeat, far from the first
        main = base[8:32, 8:32].copy()
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=10.0, angle_range_deg=(-10, 10),
        )
        assert result.status is RegistrationStatus.AMBIGUOUS_MATCH


class TestSharpnessRatio:
    def test_a_starfield_scores_much_higher_than_a_smooth_gradient(self) -> None:
        starfield = _starfield(100, 100, n_stars=30, seed=60)
        gradient = np.linspace(0.0, 1000.0, 100).reshape(1, 100) * np.ones((100, 1))
        star_ratio = _sharpness_ratio(starfield)
        gradient_ratio = _sharpness_ratio(gradient)
        assert star_ratio > 0.1
        assert gradient_ratio < 0.01
        assert star_ratio > gradient_ratio * 50

    def test_a_perfectly_flat_image_scores_zero_not_a_division_error(self) -> None:
        assert _sharpness_ratio(np.full((20, 20), 500.0)) == 0.0

    def test_a_real_match_in_an_otherwise_flat_guide_frame_is_still_found(self) -> None:
        guide = np.linspace(0.0, 2000.0, 300).reshape(1, 300) * np.ones((300, 1))
        guide[20:80, 20:80] = _starfield(60, 60, n_stars=25, seed=70) + guide[20:80, 20:80].mean()
        main = guide[30:70, 30:70].copy()
        prior_a, prior_b = _priors(1.0)

        assert _sharpness_ratio(guide) < 0.02  # whole-frame average: below the floor
        assert _sharpness_ratio(main) > 0.02  # the actual template: comfortably above it

        result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=30.0, angle_range_deg=(-30, 30),
        )
        assert result.status is RegistrationStatus.OK_OVERLAP
        assert result.confidence is not None and result.confidence > 0.9


class TestSearchDownsample:
    def test_recovers_the_approximate_location_with_downsampling(self) -> None:
        guide = _starfield(400, 400, n_stars=150, seed=30)
        main = guide[120:280, 100:300].copy()  # h=160, w=200, top-left (120, 100)
        prior_a, prior_b = _priors(1.0)

        result = _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=1, angle_step_deg=10.0, angle_range_deg=(-10, 10), search_downsample=4,
        )

        assert result.status is RegistrationStatus.OK_OVERLAP
        cx, cy = _center(result)
        assert cx == pytest.approx(100 + 200 / 2.0, abs=8.0)
        assert cy == pytest.approx(120 + 160 / 2.0, abs=8.0)
        assert result.confidence is not None and result.confidence > 0.5


class TestProgressCallback:
    def test_callback_is_called_once_per_candidate_with_a_correct_final_total(self) -> None:
        guide = _starfield(80, 80, n_stars=20, seed=20)
        main = guide[20:60, 15:65].copy()
        calls: list[tuple[int, int]] = []
        prior_a, prior_b = _priors(1.0)

        _registrar.register(
            main, guide, prior_a, prior_b,
            scale_steps=3, angle_step_deg=30.0, angle_range_deg=(-30, 30),
            progress_callback=lambda completed, total: calls.append((completed, total)),
        )

        assert len(calls) > 0
        totals = {total for _, total in calls}
        assert len(totals) == 1
        (total,) = totals
        assert [c for c, _ in calls] == list(range(1, total + 1))


class TestInputValidation:
    def test_non_2d_arrays_report_insufficient_structure_not_a_crash(self) -> None:
        guide = _starfield(50, 50, n_stars=10, seed=9)
        prior_a, prior_b = _priors(1.0)
        result = _registrar.register(np.zeros((10, 10, 3)), guide, prior_a, prior_b)
        assert result.status is RegistrationStatus.INSUFFICIENT_STRUCTURE
        assert "error" in result.diagnostics


class TestAccuracyConstants:
    def test_accuracy_targets_are_ordered_as_documented(self) -> None:
        """Issue #29 #6's own target ordering (5px desirable < 50px
        acceptable < 100px fallback) -- pins the documented constants
        against a future accidental edit, not a live accuracy measurement
        (that needs the real dataset test -- see
        test_terrestrial_registrar_real_data.py)."""
        from astrotool_core.registration.terrestrial_registrar import (
            TERRESTRIAL_DESIRABLE_ACCURACY_PX,
            TERRESTRIAL_FALLBACK_ACCURACY_PX,
        )

        assert (
            TERRESTRIAL_DESIRABLE_ACCURACY_PX
            < TERRESTRIAL_ACCEPTABLE_ACCURACY_PX
            < TERRESTRIAL_FALLBACK_ACCURACY_PX
        )
