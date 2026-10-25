"""Tests for the terrestrial-mode autofocus analyzer — issue #33's own
"Mode B" test list: static high-detail scene picks known best focus, a
locally moving (windy-tree-like) region is excluded while a static region
still drives the result, low-texture-only content is rejected, and
brightness/contrast changes alone do not move the selected optimum."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.focus.terrestrial_focus_metric import measure_terrestrial_focus

_SHAPE = (128, 128)
_TILE = 32


def _box_blur(image: np.ndarray, radius: int) -> np.ndarray:
    """A simple separable box blur (no scipy dependency, matches this
    project's own "plain numpy" convention) -- larger radius == more
    defocus, giving test scenes a controllable, monotonic blur level.

    Issue #56: edge-padded. The original zero-padded `mode="same"` convolution
    darkened a `radius`-wide band at the frame border -- a manufactured edge that
    gets STRONGER with blur, which the strongest-detail score then (rightly)
    followed. Real defocus never adds a dark frame border."""
    if radius <= 0:
        return image.astype(np.float64, copy=True)
    kernel = np.ones(2 * radius + 1, dtype=np.float64) / (2 * radius + 1)
    blurred = image.astype(np.float64, copy=True)
    for axis in (0, 1):
        blurred = np.apply_along_axis(
            lambda line: np.convolve(np.pad(line, radius, mode="edge"), kernel, mode="valid"),
            axis,
            blurred,
        )
    return blurred


def _speckle_tile(
    rng: np.random.Generator, shape: tuple[int, int], *, scale: float = 500.0
) -> np.ndarray:
    """High-frequency i.i.d. noise -- real texture, blurring it (see
    `_box_blur`) genuinely reduces its own high-frequency content, same as
    real defocus does to a real scene."""
    return rng.uniform(0.0, scale, size=shape)


def _textured_frame(
    rng: np.random.Generator, *, blur_radius: int, background: float = 1000.0
) -> np.ndarray:
    frame = np.full(_SHAPE, background, dtype=np.float64)
    for ty in range(0, _SHAPE[0], _TILE):
        for tx in range(0, _SHAPE[1], _TILE):
            frame[ty : ty + _TILE, tx : tx + _TILE] += _speckle_tile(rng, (_TILE, _TILE))
    return _box_blur(frame, blur_radius)


class TestMeasureTerrestrialFocus:
    def test_static_high_detail_scene_reports_a_usable_sharpness(self) -> None:
        rng = np.random.default_rng(1)
        frame = _textured_frame(rng, blur_radius=0)

        measurement = measure_terrestrial_focus([frame], tile_size_px=_TILE)

        assert measurement.sharpness is not None
        assert measurement.usable_tile_count > 0

    def test_a_sharper_frame_scores_higher_than_a_blurrier_one(self) -> None:
        rng = np.random.default_rng(2)
        sharp = _textured_frame(rng, blur_radius=0)
        rng = np.random.default_rng(2)  # same underlying texture, different blur
        blurry = _textured_frame(rng, blur_radius=3)

        sharp_measurement = measure_terrestrial_focus([sharp], tile_size_px=_TILE)
        blurry_measurement = measure_terrestrial_focus([blurry], tile_size_px=_TILE)

        assert sharp_measurement.sharpness is not None
        assert blurry_measurement.sharpness is not None
        assert sharp_measurement.sharpness > blurry_measurement.sharpness

    def test_a_locally_moving_tile_is_excluded_while_a_static_tile_still_drives_the_result(
        self,
    ) -> None:
        rng = np.random.default_rng(3)
        base = _textured_frame(rng, blur_radius=0)
        moved = base.copy()
        # Replace just one tile's own content entirely between samples --
        # a wind-driven-vegetation-like local scene change, everything
        # else (the "static pylon") stays identical.
        moving_rng = np.random.default_rng(999)
        moved[0:_TILE, 0:_TILE] = _speckle_tile(moving_rng, (_TILE, _TILE), scale=5000.0)

        measurement = measure_terrestrial_focus([base, moved], tile_size_px=_TILE)

        assert measurement.rejected_moving_tile_count >= 1
        assert measurement.sharpness is not None
        assert measurement.usable_tile_count > 0

    def test_a_low_texture_region_is_rejected_as_focus_evidence(self) -> None:
        rng = np.random.default_rng(4)
        frame = _textured_frame(rng, blur_radius=0)
        # Flatten one tile to a smooth sky-like region -- no real
        # high-frequency content, unlike its neighbors.
        frame[0:_TILE, 0:_TILE] = 4000.0

        measurement = measure_terrestrial_focus([frame], tile_size_px=_TILE)

        assert measurement.rejected_low_texture_tile_count >= 1

    def test_a_uniform_brightness_and_contrast_shift_does_not_change_the_ranking(self) -> None:
        rng = np.random.default_rng(5)
        sharp = _textured_frame(rng, blur_radius=0)
        rng = np.random.default_rng(5)
        blurry = _textured_frame(rng, blur_radius=3)

        # A materially different exposure/gain -- additive brightness
        # offset plus a multiplicative contrast scale -- must not flip
        # which of the two is judged sharper.
        sharp_shifted = sharp * 1.8 + 2000.0
        blurry_shifted = blurry * 1.8 + 2000.0

        sharp_measurement = measure_terrestrial_focus([sharp_shifted], tile_size_px=_TILE)
        blurry_measurement = measure_terrestrial_focus([blurry_shifted], tile_size_px=_TILE)

        assert sharp_measurement.sharpness is not None
        assert blurry_measurement.sharpness is not None
        assert sharp_measurement.sharpness > blurry_measurement.sharpness

    def test_a_scene_with_no_stable_useful_structure_reports_low_confidence(self) -> None:
        # Entirely flat -- no texture anywhere, no static high-detail
        # region to fall back on.
        frame = np.full(_SHAPE, 1000.0, dtype=np.float64)

        measurement = measure_terrestrial_focus([frame], tile_size_px=_TILE)

        assert measurement.sharpness is None
        assert measurement.confidence == 0.0


# --- issue #56: a low-texture scene under realistic sensor noise ---------------------------

_NOISE_SIGMA = 845.0  # ADU, measured on diagnostic 402eabde's ATR585M frames
_LOW_TEXTURE_SHAPE = (512, 512)
_LOW_TEXTURE_TILE = 64


def _gaussian_blur(image: np.ndarray, sigma: float) -> np.ndarray:
    """Exact (FFT) Gaussian blur -- added defocus, a known software transform."""
    if sigma <= 0.0:
        return image.astype(np.float64, copy=True)
    fy = np.fft.fftfreq(image.shape[0])[:, None]
    fx = np.fft.rfftfreq(image.shape[1])[None, :]
    kernel = np.exp(-2.0 * (np.pi * sigma) ** 2 * (fx**2 + fy**2))
    return np.fft.irfft2(np.fft.rfft2(image) * kernel, s=image.shape)


def _mast_scene() -> np.ndarray:
    """A bright featureless sky (30000 ADU) with one thin dark mast and two cross bars --
    a few sharp edges in an otherwise empty frame, like diagnostic 402eabde."""
    scene = np.full(_LOW_TEXTURE_SHAPE, 30000.0)
    scene[60:460, 250:262] = 12000.0
    scene[150:156, 200:312] = 12000.0
    scene[300:304, 220:292] = 12000.0
    return scene


def _noisy_samples(
    clean: np.ndarray, rng: np.random.Generator, count: int = 3
) -> list[np.ndarray]:
    return [clean + rng.normal(0.0, _NOISE_SIGMA, clean.shape) for _ in range(count)]


class TestLowTextureSceneUnderNoise:
    """Issue #56 (diag 402eabde): the old motion test compared a tile's change with its own
    std -- for a featureless tile that is the noise, so noise alone rejected almost every tile;
    the median score over the rest was flat under defocus."""

    def test_noise_alone_never_marks_a_tile_as_moving(self) -> None:
        rng = np.random.default_rng(56)
        samples = _noisy_samples(np.full(_LOW_TEXTURE_SHAPE, 30000.0), rng)

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        assert measurement.rejected_moving_tile_count == 0

    def test_noise_alone_on_a_structured_scene_marks_no_tile_as_moving(self) -> None:
        rng = np.random.default_rng(57)
        samples = _noisy_samples(_mast_scene(), rng)

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        assert measurement.rejected_moving_tile_count == 0

    def test_a_really_changing_tile_is_still_rejected_and_only_that_one(self) -> None:
        rng = np.random.default_rng(58)
        samples = _noisy_samples(_mast_scene(), rng)
        # Wind-blown foliage in one tile: its content is replaced between samples.
        foliage = np.random.default_rng(1)
        for sample in samples[1:]:
            sample[384:448, 0:64] = 8000.0 + foliage.uniform(0.0, 20000.0, (64, 64))

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        assert measurement.rejected_moving_tile_count == 1
        assert measurement.sharpness is not None

    def test_sub_pixel_jitter_of_a_strong_static_edge_is_not_motion(self) -> None:
        # Air shimmer: the mast shifts by a fraction of a pixel between samples. Its gradient
        # energy is shift-invariant, so it is still valid focus evidence.
        rng = np.random.default_rng(59)
        blurred = _gaussian_blur(_mast_scene(), 1.5)
        shifted = np.fft.irfft2(
            np.fft.rfft2(blurred)
            * np.exp(-2j * np.pi * 0.4 * np.fft.rfftfreq(blurred.shape[1])[None, :]),
            s=blurred.shape,
        )
        samples = [
            blurred + rng.normal(0.0, _NOISE_SIGMA, blurred.shape),
            shifted + rng.normal(0.0, _NOISE_SIGMA, blurred.shape),
        ]

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        assert measurement.rejected_moving_tile_count == 0

    def test_added_defocus_lowers_the_score_monotonically_and_clearly(self) -> None:
        rng = np.random.default_rng(60)
        scores = []
        for sigma in (0.0, 2.0, 4.0, 8.0, 16.0):
            samples = _noisy_samples(_gaussian_blur(_mast_scene(), sigma), rng)
            measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)
            assert measurement.sharpness is not None
            scores.append(measurement.sharpness)

        for sharper, blurrier in zip(scores, scores[1:], strict=False):
            assert blurrier < 0.95 * sharper, scores

    def test_featureless_noise_reports_no_confident_detail(self) -> None:
        rng = np.random.default_rng(61)
        samples = _noisy_samples(np.full(_LOW_TEXTURE_SHAPE, 30000.0), rng)

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        # Nothing rises clearly above the measured noise floor: either no score at all or a
        # small one with zero confidence -- never a confident "focus" reading.
        assert measurement.confidence == 0.0

    def test_a_scene_with_detail_reports_full_confidence(self) -> None:
        rng = np.random.default_rng(62)
        samples = _noisy_samples(_mast_scene(), rng)

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        assert measurement.confidence == 1.0

    def test_clipped_highlights_carrying_the_score_lower_the_confidence(self) -> None:
        # The detail that carries the score is clipped at the sensor maximum: how it responds
        # to focus depends on how far above the clip level it really is (unknowable).
        rng = np.random.default_rng(63)
        scene = np.full(_LOW_TEXTURE_SHAPE, 20000.0)
        scene[200:240, 200:240] = 90000.0  # a glint far above the clip level
        samples = [np.minimum(sample, 65520.0) for sample in _noisy_samples(scene, rng)]

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        assert measurement.sharpness is not None
        assert measurement.confidence < 0.5

    def test_the_counts_partition_every_tile(self) -> None:
        rng = np.random.default_rng(64)
        samples = _noisy_samples(_mast_scene(), rng)
        for sample in samples[1:]:
            sample[0:64, 0:64] = rng.uniform(0.0, 60000.0, (64, 64))

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        tiles = (_LOW_TEXTURE_SHAPE[0] // _LOW_TEXTURE_TILE) * (
            _LOW_TEXTURE_SHAPE[1] // _LOW_TEXTURE_TILE
        )
        assert (
            measurement.usable_tile_count
            + measurement.rejected_moving_tile_count
            + measurement.rejected_low_texture_tile_count
        ) == tiles


# --- issue #56 review F1/F4/F5/F8: sample-set independence, foliage, pinned constants -------

_BINNED_NOISE_VARIANCE = (_NOISE_SIGMA / 4.0) ** 2  # 4x4 block mean of white noise
_SOBEL_WHITE_NOISE_ENERGY = 24.0 * _BINNED_NOISE_VARIANCE  # 2 x (1+4+1+1+4+1) x variance


def _foliage(rng: np.random.Generator, shape: tuple[int, int]) -> np.ndarray:
    """Wind-blown leaves: new content in every sample, with its own (random) motion blur --
    its sharpness says nothing about the focuser position."""
    leaf_px = 8  # leaves a few pixels across, not single-pixel grain
    blocks = rng.uniform(-8000.0, 8000.0, (shape[0] // leaf_px, shape[1] // leaf_px))
    leaves = 9000.0 + np.kron(blocks, np.ones((leaf_px, leaf_px)))
    return _gaussian_blur(leaves, float(rng.uniform(0.0, 1.5)))


class TestOneScorePerPosition:
    """Review F1: the score at one focuser position must not depend on sample order or on
    duplicated re-reads of the same frame."""

    def test_sample_order_does_not_change_anything(self) -> None:
        rng = np.random.default_rng(70)
        a, b, c = _noisy_samples(_mast_scene(), rng)

        first = measure_terrestrial_focus([a, b, c], tile_size_px=_LOW_TEXTURE_TILE)
        for order in ([c, a, b], [b, c, a], [c, b, a]):
            assert measure_terrestrial_focus(order, tile_size_px=_LOW_TEXTURE_TILE) == first

    def test_a_duplicated_sample_counts_once(self) -> None:
        rng = np.random.default_rng(71)
        a, b = _noisy_samples(_mast_scene(), rng, count=2)

        assert measure_terrestrial_focus(
            [a, a.copy(), b], tile_size_px=_LOW_TEXTURE_TILE
        ) == measure_terrestrial_focus([a, b], tile_size_px=_LOW_TEXTURE_TILE)
        assert measure_terrestrial_focus(
            [a, a, a], tile_size_px=_LOW_TEXTURE_TILE
        ) == measure_terrestrial_focus([a], tile_size_px=_LOW_TEXTURE_TILE)


class TestFoliage:
    """Review F4: swaying foliage over a quarter of the frame, distinct samples."""

    def test_foliage_is_rejected_and_the_curve_stays_monotone(self) -> None:
        rng = np.random.default_rng(72)
        quarter = (slice(256, 512), slice(0, 256))  # 16 of the 64 tiles
        scores = []
        for sigma in (0.0, 2.0, 4.0, 8.0, 16.0):
            samples = []
            for _ in range(3):
                # The leaves are as far away as the mast: defocused with it, plus their own
                # motion, and different in every sample.
                scene = _mast_scene()
                scene[quarter] = _foliage(rng, (256, 256))
                samples.append(
                    _gaussian_blur(scene, sigma) + rng.normal(0.0, _NOISE_SIGMA, scene.shape)
                )
            measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)
            if sigma <= 2.0:
                # While the leaves are sharp (strong, changing detail) every foliage tile is
                # rejected; defocused further they carry little energy and their change sinks
                # towards the noise, so they may stay -- the curve below must not care.
                assert measurement.rejected_moving_tile_count == 16, (sigma, measurement)
            assert measurement.sharpness is not None
            scores.append(measurement.sharpness)

        for sharper, blurrier in zip(scores, scores[1:], strict=False):
            assert blurrier < 0.95 * sharper, scores


class TestNoiseFloor:
    def test_pure_noise_scores_near_zero_after_the_measured_noise_floor(self) -> None:
        # The floor is measured from N distinct samples: what remains of pure noise is only
        # the spread of tile energies, a small fraction of the noise energy itself.
        rng = np.random.default_rng(73)
        for count in (2, 3, 5):
            samples = _noisy_samples(np.full(_LOW_TEXTURE_SHAPE, 30000.0), rng, count=count)
            measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)
            assert (
                measurement.sharpness is None
                or measurement.sharpness < 0.5 * _SOBEL_WHITE_NOISE_ENERGY
            ), (count, measurement)

    def test_the_floor_is_the_white_noise_energy_of_one_binned_sample(self) -> None:
        from astrotool_core.focus.terrestrial_focus_metric import _bin, _noise_floor, _per_tile

        rng = np.random.default_rng(74)
        for count in (2, 3, 5):
            samples = _noisy_samples(np.full(_LOW_TEXTURE_SHAPE, 30000.0), rng, count=count)
            binned = [_bin(sample, 4) for sample in samples]
            energy = np.full((8, 8), 1e12)  # no cap from the tile energies
            floor = _noise_floor(binned, 16, np.zeros((8, 8), dtype=bool), energy)
            assert floor == pytest.approx(_SOBEL_WHITE_NOISE_ENERGY, rel=0.1), count
            assert _per_tile(binned[0], 16, "mean").shape == (8, 8)

    def test_a_brighter_noisier_region_is_not_motion(self) -> None:
        # Shot noise: a region 4x brighter has 2x the noise of the rest -- noise, not motion.
        rng = np.random.default_rng(75)
        samples = _noisy_samples(_mast_scene(), rng)
        for sample in samples:
            sample[0:256, 256:512] += rng.normal(0.0, np.sqrt(3.0) * _NOISE_SIGMA, (256, 256))

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        assert measurement.rejected_moving_tile_count == 0


class TestHotPixels:
    def test_single_pixel_hot_pixels_do_not_drown_the_focus_slope(self) -> None:
        # Fixed sensor hot pixels do not defocus. Block-mean binning keeps them from carrying
        # the strongest-detail score, so the scene's own slope stays visible.
        rng = np.random.default_rng(76)
        hot = np.zeros(_LOW_TEXTURE_SHAPE)
        hot[rng.integers(0, 512, 400), rng.integers(0, 512, 400)] = 30000.0
        scores = []
        for sigma in (0.0, 2.0, 4.0, 8.0):
            clean = _gaussian_blur(_mast_scene(), sigma) + hot
            measurement = measure_terrestrial_focus(
                _noisy_samples(clean, rng), tile_size_px=_LOW_TEXTURE_TILE
            )
            assert measurement.sharpness is not None
            scores.append(measurement.sharpness)

        for sharper, blurrier in zip(scores, scores[1:], strict=False):
            assert blurrier < 0.95 * sharper, scores


class TestConfidence:
    @staticmethod
    def _faint_bars(peak: float) -> np.ndarray:
        """Five faint vertical bars (5 x 2 tiles of real, unclipped detail at ~8x the noise
        floor) and a small glint of `peak` ADU in another tile."""
        scene = np.full(_LOW_TEXTURE_SHAPE, 20000.0)
        for column in (40, 104, 168, 232, 296):
            scene[64:192, column : column + 8] = 22000.0
        scene[400:416, 400:416] = peak
        return scene

    def test_faint_unclipped_detail_well_above_the_floor_is_fully_confident(self) -> None:
        rng = np.random.default_rng(77)
        samples = _noisy_samples(self._faint_bars(peak=40000.0), rng)

        measurement = measure_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        assert measurement.sharpness is not None
        assert measurement.confidence == 1.0

    def test_only_the_clipped_share_lowers_it_when_a_glint_clips(self) -> None:
        from astrotool_core.focus.terrestrial_focus_metric import analyze_terrestrial_focus

        rng = np.random.default_rng(78)
        samples = [
            np.minimum(sample, 65520.0)
            for sample in _noisy_samples(self._faint_bars(peak=200000.0), rng)
        ]

        evidence = analyze_terrestrial_focus(samples, tile_size_px=_LOW_TEXTURE_TILE)

        # The bars still give >= 3 detail tiles; the clipped glint carries most of the score.
        assert evidence.measurement.usable_tile_count >= 3
        assert evidence.clipped_share > 0.5
        assert evidence.measurement.confidence == pytest.approx(1.0 - evidence.clipped_share)


class TestNonFinite:
    def test_a_nan_or_inf_frame_gives_no_score(self) -> None:
        for bad in (np.nan, np.inf):
            frame = _mast_scene()
            frame[10, 10] = bad
            measurement = measure_terrestrial_focus([frame], tile_size_px=_LOW_TEXTURE_TILE)
            assert measurement.sharpness is None
            assert measurement.confidence == 0.0


class TestAirShimmer:
    """Review F1, synthetic (CI has no real frames): air shimmer shifts a strong static edge
    between samples. It is not motion -- the content is the same, its gradient energy is
    shift-invariant -- so neither the rejected set nor the score may depend on which of the
    samples were measured."""

    @staticmethod
    def _shimmering_samples() -> list[np.ndarray]:
        rng = np.random.default_rng(80)
        sharp = _gaussian_blur(_mast_scene(), 1.0)
        fx = np.fft.rfftfreq(sharp.shape[1])[None, :]
        samples = []
        # Whole 4x4 bins: the binned content is identical up to noise, only displaced.
        for shift_px in (0.0, 4.0, -4.0):
            shifted = np.fft.irfft2(
                np.fft.rfft2(sharp) * np.exp(-2j * np.pi * shift_px * fx), s=sharp.shape
            )
            samples.append(shifted + rng.normal(0.0, _NOISE_SIGMA, sharp.shape))
        return samples

    def test_a_shimmering_edge_is_not_rejected_as_motion(self) -> None:
        measurement = measure_terrestrial_focus(
            self._shimmering_samples(), tile_size_px=_LOW_TEXTURE_TILE
        )

        assert measurement.rejected_moving_tile_count == 0

    def test_the_score_does_not_depend_on_which_shimmering_samples_were_measured(
        self,
    ) -> None:
        a, b, c = self._shimmering_samples()
        scores = [
            measure_terrestrial_focus(subset, tile_size_px=_LOW_TEXTURE_TILE).sharpness
            for subset in ([a], [b], [c], [a, b], [b, c], [a, b, c])
        ]

        assert all(score is not None for score in scores)
        values = [float(score) for score in scores if score is not None]
        assert max(values) / min(values) - 1.0 < 0.05, values
