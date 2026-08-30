"""Tests for fov_registration — locating the main camera's actual
footprint within a guide frame by content matching (rotation, scale,
translation), replacing fov_overlay's config-only centered placeholder.

Synthetic starfields (a flat background plus several Gaussian "stars" at
random positions) stand in for real sky frames: they have genuine local
structure (unlike a flat test pattern, which correlates everywhere
equally) without needing real captured FITS data.
"""

from __future__ import annotations

import numpy as np
import pytest
from collimation_tool.ui.fov_registration import (
    _resize_bilinear,
    _rotate_bilinear,
    register_main_frame_in_guide_frame,
    registration_corners,
)


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


class TestTranslationOnly:
    def test_recovers_the_exact_crop_location(self) -> None:
        guide = _starfield(200, 200, n_stars=60, seed=1)
        main = guide[80:140, 60:130].copy()  # h=60, w=70, top-left (80, 60)

        result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=5.0,
            angle_range_deg=(-5, 5),
        )

        assert result is not None
        assert result.center_x_px == pytest.approx(60 + 70 / 2.0, abs=0.5)
        assert result.center_y_px == pytest.approx(80 + 60 / 2.0, abs=0.5)
        assert result.rotation_deg == pytest.approx(0.0, abs=1e-6)
        assert result.scale == pytest.approx(1.0)
        assert result.score > 0.99  # an exact pixel-for-pixel crop should score ~1.0


class TestRotation:
    def test_recovers_a_known_rotation_and_reconstructs_the_crop(self) -> None:
        guide = _starfield(200, 200, n_stars=60, seed=2)
        # A generously-margined patch, rotated by a known angle, then
        # cropped back down — avoids the empty corners a rotation
        # introduces at the very edges.
        patch = guide[50:150, 40:170].copy()  # 100x130
        rotated = _rotate_bilinear(patch, angle_deg=25.0, fill_value=float(patch.mean()))
        cy0 = rotated.shape[0] // 2 - 25
        cx0 = rotated.shape[1] // 2 - 30
        main = rotated[cy0 : cy0 + 50, cx0 : cx0 + 60]

        result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=2.0,
            angle_range_deg=(-180, 180),
        )

        assert result is not None
        assert result.rotation_deg == pytest.approx(-25.0, abs=2.0)
        assert result.score > 0.95

    def test_rotation_and_corners_are_self_consistent(self) -> None:
        """The definitive correctness check: rotating the guide frame at
        the found location by -rotation_deg and cropping to
        (width_px, height_px) around the center must reproduce the
        original main image — independent of any sign-convention
        confusion about what "rotation_deg" intuitively means."""
        guide = _starfield(200, 200, n_stars=60, seed=3)
        patch = guide[40:160, 30:180].copy()  # 120x150
        rotated = _rotate_bilinear(patch, angle_deg=-40.0, fill_value=float(patch.mean()))
        cy0 = rotated.shape[0] // 2 - 30
        cx0 = rotated.shape[1] // 2 - 35
        main = rotated[cy0 : cy0 + 60, cx0 : cx0 + 70]

        result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=2.0,
            angle_range_deg=(-180, 180),
        )
        assert result is not None

        half_h, half_w = int(result.height_px), int(result.width_px)
        cx, cy = result.center_x_px, result.center_y_px
        crop = guide[int(cy - half_h) : int(cy + half_h), int(cx - half_w) : int(cx + half_w)]
        unrotated = _rotate_bilinear(
            crop, angle_deg=-result.rotation_deg, fill_value=float(crop.mean())
        )
        ch, cw = unrotated.shape
        cy2, cx2 = ch // 2 - main.shape[0] // 2, cw // 2 - main.shape[1] // 2
        reconstructed = unrotated[cy2 : cy2 + main.shape[0], cx2 : cx2 + main.shape[1]]

        # Near-exact — small interpolation error only.
        assert np.mean(np.abs(reconstructed - main)) < 1.0

    def test_registration_corners_form_a_rectangle_of_the_right_size(self) -> None:
        from collimation_tool.ui.fov_registration import FovRegistrationResult

        result = FovRegistrationResult(
            center_x_px=100.0, center_y_px=50.0, width_px=40.0, height_px=20.0,
            rotation_deg=0.0, scale=1.0, score=1.0,
        )
        corners = registration_corners(result)
        assert len(corners) == 4
        # Unrotated: an axis-aligned rectangle centered at (100, 50).
        assert corners[0] == pytest.approx((80.0, 40.0))  # top-left
        assert corners[2] == pytest.approx((120.0, 60.0))  # bottom-right


class TestScale:
    def test_recovers_a_known_scale_ratio(self) -> None:
        guide = _starfield(220, 220, n_stars=80, seed=4)
        crop = guide[70:150, 60:180]  # h=80, w=120
        main = _resize_bilinear(crop, 27, 40)  # roughly scale 1/3

        result = register_main_frame_in_guide_frame(
            main, guide, approx_scale=3.0, scale_search_fraction=0.3, scale_steps=7,
            angle_step_deg=3.0, angle_range_deg=(-6, 6),
        )

        assert result is not None
        assert result.scale == pytest.approx(3.0, abs=0.3)
        assert result.center_x_px == pytest.approx(60 + 120 / 2.0, abs=2.0)
        assert result.center_y_px == pytest.approx(70 + 80 / 2.0, abs=2.0)

    def test_scale_steps_one_trusts_approx_scale_exactly(self) -> None:
        guide = _starfield(150, 150, n_stars=40, seed=5)
        main = guide[40:90, 50:110].copy()

        result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=5.0,
            angle_range_deg=(-5, 5),
        )
        assert result is not None
        assert result.scale == 1.0  # no search performed — the given value, verbatim


class TestExposureInvariance:
    def test_a_brightness_and_contrast_difference_does_not_affect_the_match(self) -> None:
        """The two cameras run at different exposures/gains by design
        (see the FOV-overlay feature request) — NCC must be unaffected
        by a purely linear brightness/contrast difference."""
        guide = _starfield(200, 200, n_stars=60, seed=6)
        main = guide[80:140, 60:130].copy()
        dimmer_main = main * 0.15 + 40.0  # much dimmer, different offset

        bright_result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=5.0,
            angle_range_deg=(-5, 5),
        )
        dim_result = register_main_frame_in_guide_frame(
            dimmer_main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=5.0,
            angle_range_deg=(-5, 5),
        )

        assert bright_result is not None
        assert dim_result is not None
        assert dim_result.center_x_px == pytest.approx(bright_result.center_x_px)
        assert dim_result.center_y_px == pytest.approx(bright_result.center_y_px)
        assert dim_result.score == pytest.approx(bright_result.score, abs=1e-6)


class TestNoConfidentMatch:
    def test_unrelated_content_returns_none(self) -> None:
        guide = _starfield(200, 200, n_stars=60, seed=7)
        rng = np.random.default_rng(99)
        unrelated = rng.normal(500.0, 50.0, size=(60, 70))

        result = register_main_frame_in_guide_frame(
            unrelated,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=10.0,
            angle_range_deg=(-30, 30),
        )
        assert result is None

    def test_a_flat_template_returns_none_rather_than_a_meaningless_match(self) -> None:
        guide = _starfield(120, 120, n_stars=30, seed=8)
        flat = np.full((30, 30), 500.0)

        result = register_main_frame_in_guide_frame(
            flat,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=10.0,
            angle_range_deg=(-30, 30),
        )
        assert result is None

    def test_a_featureless_region_of_a_real_scene_returns_none(self) -> None:
        """Real-world bug: a guide frame showing sky above a landscape
        crushed dark by exposure tuned for the much brighter sky (a
        genuine daytime/twilight test scene, not a pure starfield) let a
        near-flat main-camera crop score a spuriously "confident" match
        somewhere with no real content — dividing two near-zero
        correlation terms (a near-flat template's tiny residual noise
        against an equally near-flat window) is numerically unstable and
        can land anywhere. The main crop here has genuine but tiny
        (0.5 ADU) noise — enough to dodge the *perfectly* flat template's
        own zero-variance short-circuit, which is exactly the gap the
        contrast floor exists to close."""
        rng = np.random.default_rng(11)
        guide = np.full((120, 200), 800.0)  # bright "sky"
        guide[70:, :] = 50.0  # dark "landscape" — no stars/texture here
        # Real stars, sky half only.
        guide += _starfield(120, 200, n_stars=25, seed=11) - 100.0
        # Landscape stays ~flat (tiny noise only).
        guide[70:, :] = 50.0 + rng.normal(0.0, 0.5, size=guide[70:, :].shape)

        # A main-camera crop pointed entirely at the landscape: almost no
        # real structure to match against, unlike the starry sky above it.
        featureless_main = guide[80:110, 60:140].copy()

        result = register_main_frame_in_guide_frame(
            featureless_main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=15.0,
            angle_range_deg=(-30, 30),
        )
        assert result is None


class TestSearchDownsample:
    """See the real-world "very slow" report: search_downsample trades a
    bounded amount of position precision for a large (roughly quadratic)
    speedup — see the module docstring's "Performance"."""

    def test_recovers_the_approximate_location_with_downsampling(self) -> None:
        guide = _starfield(400, 400, n_stars=150, seed=30)
        main = guide[120:280, 100:300].copy()  # h=160, w=200, top-left (120, 100)

        result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=10.0,
            angle_range_deg=(-10, 10),
            search_downsample=4,
        )

        assert result is not None
        # Position precision degrades by up to the downsample factor —
        # not exact like the sample_downsample=1 (default) tests above.
        assert result.center_x_px == pytest.approx(100 + 200 / 2.0, abs=8.0)
        assert result.center_y_px == pytest.approx(120 + 160 / 2.0, abs=8.0)
        assert result.score > 0.5

    def test_downsample_of_one_is_the_no_downsampling_default(self) -> None:
        guide = _starfield(200, 200, n_stars=60, seed=31)
        main = guide[80:140, 60:130].copy()

        with_default = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=5.0,
            angle_range_deg=(-5, 5),
        )
        with_explicit_one = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=5.0,
            angle_range_deg=(-5, 5),
            search_downsample=1,
        )

        assert with_default is not None
        assert with_explicit_one is not None
        assert with_default.center_x_px == with_explicit_one.center_x_px
        assert with_default.center_y_px == with_explicit_one.center_y_px

    def test_a_non_positive_downsample_is_treated_as_one(self) -> None:
        guide = _starfield(100, 100, n_stars=30, seed=32)
        main = guide[30:70, 20:80].copy()

        result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=5.0,
            angle_range_deg=(-5, 5),
            search_downsample=0,
        )
        assert result is not None
        assert result.center_x_px == pytest.approx(20 + 60 / 2.0, abs=0.5)


class TestProgressCallback:
    """See the real bug this was added for: "Calibration started but
    working without any status on progress" — a ~2-minute search with no
    feedback looked indistinguishable from a hang."""

    def test_callback_is_called_once_per_candidate_with_a_correct_final_total(self) -> None:
        guide = _starfield(80, 80, n_stars=20, seed=20)
        main = guide[20:60, 15:65].copy()
        calls: list[tuple[int, int]] = []

        register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=3,
            angle_step_deg=30.0,
            angle_range_deg=(-30, 30),
            progress_callback=lambda completed, total: calls.append((completed, total)),
        )

        assert len(calls) > 0
        # completed counts strictly increase by 1 each call, up to total.
        totals = {total for _, total in calls}
        assert len(totals) == 1  # the same total reported throughout
        (total,) = totals
        assert [c for c, _ in calls] == list(range(1, total + 1))

    def test_no_callback_given_does_not_raise(self) -> None:
        guide = _starfield(60, 60, n_stars=10, seed=21)
        main = guide[10:40, 10:45].copy()
        # Must work with the default progress_callback=None, same as
        # every other test in this file already exercises implicitly.
        result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=10.0,
            angle_range_deg=(-10, 10),
        )
        assert result is not None

    def test_callback_never_called_when_every_scale_is_too_large(self) -> None:
        guide = _starfield(50, 50, n_stars=10, seed=22)
        main = np.full((80, 80), 500.0)
        calls: list[tuple[int, int]] = []

        result = register_main_frame_in_guide_frame(
            main,
            guide,
            approx_scale=1.0,
            scale_steps=1,
            angle_step_deg=30.0,
            progress_callback=lambda completed, total: calls.append((completed, total)),
        )
        assert result is None
        assert calls == []


class TestInputValidation:
    def test_non_2d_arrays_raise(self) -> None:
        guide = _starfield(50, 50, n_stars=10, seed=9)
        with pytest.raises(ValueError):
            register_main_frame_in_guide_frame(np.zeros((10, 10, 3)), guide, approx_scale=1.0)

    def test_non_positive_approx_scale_raises(self) -> None:
        guide = _starfield(50, 50, n_stars=10, seed=10)
        main = guide[10:30, 10:30]
        with pytest.raises(ValueError):
            register_main_frame_in_guide_frame(main, guide, approx_scale=0.0)

    def test_a_template_too_large_for_every_candidate_scale_returns_none(self) -> None:
        guide = _starfield(50, 50, n_stars=10, seed=11)
        main = np.full((80, 80), 500.0)  # bigger than guide on both axes
        result = register_main_frame_in_guide_frame(
            main, guide, approx_scale=1.0, scale_steps=1, angle_step_deg=30.0
        )
        assert result is None


class TestResizeAndRotateHelpers:
    """Pure-numpy stand-ins for cv2.resize/cv2.rotate this module relies
    on — this project doesn't depend on OpenCV/SciPy/Pillow."""

    def test_resize_to_the_same_size_is_a_no_op(self) -> None:
        image = np.arange(16.0).reshape(4, 4)
        resized = _resize_bilinear(image, 4, 4)
        assert np.array_equal(resized, image)

    def test_resize_preserves_a_uniform_value(self) -> None:
        image = np.full((10, 10), 42.0)
        resized = _resize_bilinear(image, 3, 5)
        assert np.allclose(resized, 42.0)

    def test_rotate_by_zero_degrees_is_a_no_op(self) -> None:
        image = np.arange(16.0).reshape(4, 4)
        rotated = _rotate_bilinear(image, 0.0, fill_value=0.0)
        assert np.array_equal(rotated, image)

    def test_rotate_by_360_degrees_is_a_no_op(self) -> None:
        image = np.arange(16.0).reshape(4, 4)
        rotated = _rotate_bilinear(image, 360.0, fill_value=0.0)
        assert np.array_equal(rotated, image)

    def test_rotate_preserves_a_uniform_value_at_the_center(self) -> None:
        image = np.full((20, 20), 7.0)
        rotated = _rotate_bilinear(image, 33.0, fill_value=0.0)
        assert rotated[10, 10] == pytest.approx(7.0)
