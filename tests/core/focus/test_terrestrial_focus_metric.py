"""Tests for the terrestrial-mode autofocus analyzer — issue #33's own
"Mode B" test list: static high-detail scene picks known best focus, a
locally moving (windy-tree-like) region is excluded while a static region
still drives the result, low-texture-only content is rejected, and
brightness/contrast changes alone do not move the selected optimum."""

from __future__ import annotations

import numpy as np
from astrotool_core.focus.terrestrial_focus_metric import measure_terrestrial_focus

_SHAPE = (128, 128)
_TILE = 32


def _box_blur(image: np.ndarray, radius: int) -> np.ndarray:
    """A simple separable box blur (no scipy dependency, matches this
    project's own "plain numpy" convention) -- larger radius == more
    defocus, giving test scenes a controllable, monotonic blur level."""
    if radius <= 0:
        return image.astype(np.float64, copy=True)
    kernel = np.ones(2 * radius + 1, dtype=np.float64) / (2 * radius + 1)
    blurred = image.astype(np.float64, copy=True)
    for axis in (0, 1):
        blurred = np.apply_along_axis(
            lambda line: np.convolve(line, kernel, mode="same"), axis, blurred
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
