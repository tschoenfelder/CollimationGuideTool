"""Terrestrial-mode autofocus analyzer — issue #33's own "Mode B"
requirement: a separate image-analysis implementation, robust to realistic
terrestrial scenes (wind-driven vegetation, static structures, low-texture
sky, exposure differences), that must not simply maximize one whole-frame
sharpness number if moving content can dominate it.

Approach: split each frame into fixed-size tiles, score each tile's own
Sobel-gradient ("Tenengrad") energy relative to its own variance (same
"ratio to variance so raw contrast/brightness can't fake sharpness"
convention `terrestrial_registrar._sharpness_ratio` already established in
this codebase, applied here as a genuinely optimizable per-tile scalar
rather than a pass/fail gate), reject low-texture tiles (a floor relative
to the sharpest tile in the SAME frame, so this self-calibrates across
scenes/exposures rather than needing one absolute magic constant), and --
given 2+ same-position samples -- reject any tile whose own content
changed materially between them (wind-driven local scene change) before
aggregating the remaining static, textured tiles via a robust median.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: A tile's own Tenengrad/variance ratio must be at least this fraction of
#: the sharpest tile's own ratio in the same sample to count as usable
#: focus evidence -- self-calibrating (no absolute, brightness-scale-
#: dependent constant), rejects flat/low-texture regions like open sky.
_LOW_TEXTURE_RATIO = 0.05

#: A tile whose own content changed by more than this fraction of its own
#: (static-sample) standard deviation, between the first and any later
#: sample, is treated as locally moving (wind-driven vegetation) and
#: excluded from this measurement.
_MOTION_RATIO_THRESHOLD = 0.5

#: Usable-tile count at which aggregate confidence reaches 1.0 -- mirrors
#: star_focus_metric's own `_FULL_CONFIDENCE_STAR_COUNT` convention.
_FULL_CONFIDENCE_TILE_COUNT = 3


@dataclass(frozen=True)
class TerrestrialFocusMeasurement:
    """`sharpness` is the robust (median) Tenengrad/variance-ratio
    aggregate across every usable (static, sufficiently textured) tile --
    `None` if no tile cleared every rejection guard."""

    sharpness: float | None
    confidence: float
    usable_tile_count: int
    rejected_moving_tile_count: int
    rejected_low_texture_tile_count: int


def _sobel_gradients(tile: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """3x3 Sobel gradients via padded-array slicing -- plain numpy, no
    scipy/OpenCV dependency, matching `terrestrial_registrar`'s own
    established convention for this project."""
    padded = np.pad(tile, 1, mode="edge")
    gx = (
        padded[:-2, 2:] + 2.0 * padded[1:-1, 2:] + padded[2:, 2:]
        - padded[:-2, :-2] - 2.0 * padded[1:-1, :-2] - padded[2:, :-2]
    )
    gy = (
        padded[2:, :-2] + 2.0 * padded[2:, 1:-1] + padded[2:, 2:]
        - padded[:-2, :-2] - 2.0 * padded[:-2, 1:-1] - padded[:-2, 2:]
    )
    return gx, gy


def _tenengrad_ratio(tile: np.ndarray) -> float:
    """Sobel-gradient energy divided by the tile's own variance -- offset-
    invariant by construction (a constant additive shift leaves every
    gradient at 0) and scale-invariant (both the numerator and denominator
    scale as the square of a multiplicative contrast factor), so exposure/
    gain differences between samples don't fake or hide real sharpness."""
    variance = float(tile.var())
    if variance <= 0.0:
        return 0.0
    gx, gy = _sobel_gradients(tile.astype(np.float64))
    energy = float(np.mean(gx**2 + gy**2))
    return energy / variance


def _iter_tiles(
    shape: tuple[int, int], tile_size_px: int
) -> list[tuple[slice, slice]]:
    height, width = shape
    tiles = []
    for top in range(0, height - tile_size_px + 1, tile_size_px):
        for left in range(0, width - tile_size_px + 1, tile_size_px):
            tiles.append((slice(top, top + tile_size_px), slice(left, left + tile_size_px)))
    return tiles


def measure_terrestrial_focus(
    frames: Sequence[np.ndarray], *, tile_size_px: int = 64
) -> TerrestrialFocusMeasurement:
    """`frames` are 2D mono analysis planes, all from the SAME focuser
    position (one or more samples -- 2+ lets moving tiles be identified
    and excluded; a single frame skips motion rejection entirely, since
    there is nothing to compare against)."""
    reference = frames[0]
    tiles = _iter_tiles(reference.shape, tile_size_px)

    ratios: list[float] = []
    rejected_moving = 0
    rejected_low_texture = 0

    per_tile_ratios: list[tuple[tuple[slice, slice], float]] = []
    for rows, cols in tiles:
        ref_tile = reference[rows, cols]
        if len(frames) > 1:
            ref_std = float(ref_tile.std())
            moved = False
            if ref_std > 0.0:
                for other in frames[1:]:
                    other_tile = other[rows, cols]
                    change = float(np.mean(np.abs(other_tile - ref_tile)))
                    if change / ref_std > _MOTION_RATIO_THRESHOLD:
                        moved = True
                        break
            if moved:
                rejected_moving += 1
                continue
        per_tile_ratios.append(((rows, cols), _tenengrad_ratio(ref_tile)))

    if not per_tile_ratios:
        return TerrestrialFocusMeasurement(
            sharpness=None, confidence=0.0, usable_tile_count=0,
            rejected_moving_tile_count=rejected_moving,
            rejected_low_texture_tile_count=rejected_low_texture,
        )

    max_ratio = max(ratio for _, ratio in per_tile_ratios)
    if max_ratio <= 0.0:
        # Not even the sharpest tile has any real gradient energy -- a
        # fully flat/textureless frame, not merely "some tiles are
        # smoother than others" (the floor-relative-to-max-ratio check
        # below only makes sense once at least one tile has real signal).
        return TerrestrialFocusMeasurement(
            sharpness=None, confidence=0.0, usable_tile_count=0,
            rejected_moving_tile_count=rejected_moving,
            rejected_low_texture_tile_count=rejected_low_texture + len(per_tile_ratios),
        )
    texture_floor = max_ratio * _LOW_TEXTURE_RATIO
    for _, ratio in per_tile_ratios:
        if ratio < texture_floor:
            rejected_low_texture += 1
        else:
            ratios.append(ratio)

    if not ratios:
        return TerrestrialFocusMeasurement(
            sharpness=None, confidence=0.0, usable_tile_count=0,
            rejected_moving_tile_count=rejected_moving,
            rejected_low_texture_tile_count=rejected_low_texture,
        )

    sharpness = float(np.median(ratios))
    confidence = min(1.0, len(ratios) / _FULL_CONFIDENCE_TILE_COUNT)
    return TerrestrialFocusMeasurement(
        sharpness=sharpness, confidence=confidence, usable_tile_count=len(ratios),
        rejected_moving_tile_count=rejected_moving,
        rejected_low_texture_tile_count=rejected_low_texture,
    )
