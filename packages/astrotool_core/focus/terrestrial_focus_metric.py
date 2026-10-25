"""Terrestrial-mode autofocus analyzer — issue #33's own "Mode B"
requirement: a separate image-analysis implementation, robust to realistic
terrestrial scenes (wind-driven vegetation, static structures, low-texture
sky, exposure differences), that must not simply maximize one whole-frame
sharpness number if moving content can dominate it.

Approach (issue #56): block-mean bin each frame (noise-robust), split it into
fixed-size tiles, score each tile's Sobel-gradient ("Tenengrad") energy,
reject tiles whose content really changed between same-position samples
(wind-driven local scene change), and follow the strongest-detail static
tiles above the frame's own noise floor -- so a mostly featureless scene
(one mast against a bright sky) still gives a monotone focus curve.

Issue #35's real field failure (diagnostic UUID
73a007b6-6c9b-41e2-a3e5-66a21ec71ffd, plus a live ±500-step rig sweep run
as that issue's own follow-up): this previously divided each tile's
Tenengrad energy by its own variance ("ratio to variance so raw contrast/
brightness can't fake sharpness", mirroring `terrestrial_registrar.
_sharpness_ratio`'s own convention). Real sweep data proved that
normalization actively **inverts** the true focus signal for real scenes
whose local variance is itself driven by genuine edge/texture content
(as most real scenes are) rather than by exposure/gain alone: across a
real 21-position sweep, the ratio was *minimized* almost exactly at the
independently-verified true best focus and rose toward both edges, while
raw (unnormalized) gradient energy peaked cleanly and unimodally right at
that same true best focus. Since `AutofocusController` already freezes
exposure/gain for the whole search (`set_auto_exposure_paused(True)`),
the cross-sample exposure-invariance this normalization was meant to buy
was never actually needed by this function's one real caller -- raw
gradient energy is used directly now. See `datasets/regressions/35/` for
the real frames and the full analysis.

Issue #56's real field failures (diagnostics 402eabde-d730-43b0-823d-a7f47c61f944
and f0bc314b-a889-4e18-ac9b-74971f42f014, `datasets/regressions/56*/`): on a
low-texture scene (noise sigma ~840 ADU) the old metric was noise-dominated.
(1) Its motion test compared a tile's frame-to-frame change with the tile's
own std, which for a featureless tile *is* the noise: noise alone
(mean |diff| ~ 1.13 sigma) beat the 0.5 threshold and ~90 % of the tiles were
rejected as "moving". (2) The score was the median over the remaining,
mostly sky-noise tiles -- flat (< 20 %) and non-monotone under up to 32 px of
added blur, so the search never saw a 5 % improvement and "succeeded"
without moving. Now:

- frames are block-mean binned (`_BIN_FACTOR`) before any measurement --
  noise sigma drops by the bin factor while defocus-scale detail stays;
- identical samples count once (a re-read of the same frame is no new sample);
- a tile is *moving* only when its largest change between ANY two samples
  clearly exceeds the frame's own noise-only change (the median tile change,
  `_MOTION_NOISE_FACTOR`) AND its content was replaced, not merely jittered
  (`_MOTION_RATIO_THRESHOLD` x its std) -- noise alone never rejects, air
  shimmer and flickering glints on a static edge (its energy is
  shift-invariant) are not rejected, really replaced content still is;
- each tile's energy is the mean of its per-sample energies, so the result
  does not depend on sample order (review F1: measuring change only against
  the first sample and the energy of the averaged frame made one position's
  score swing 27-58 % with the sample set; now <= 4.4 % on the real frames,
  which is the frames' own frame-to-frame variation);
- the score is the mean energy of the strongest-detail static tiles
  (`_TOP_DETAIL_FRACTION`) minus the energy the sensor noise alone
  contributes to one sample (measured from the sample differences,
  `_noise_floor`): it follows the few tiles that carry focus information
  instead of the many that carry only noise, and its relative change per
  focuser step is not diluted by a constant noise offset (the search's 5 %
  improvement margin is relative). `rejected_low_texture_tile_count` now
  counts the static tiles below that top-detail selection.

Measured on the two bundles' real frames + exact Gaussian blur (the regression
test's sweep): 402eabde 0/2/4/8/16/32 px -> 2.83e8, 2.73e8, 2.53e8, 2.04e8,
1.28e8, 5.35e7 (old: 1.71e7, 2.02e7, 2.02e7, 2.01e7, 1.98e7, 1.90e7); 0 tiles
moving on noise alone (old: all 1980).

Saturation: in both #56 bundles the clipped glints sit on the mast, the only
detail, and carry 72-93 % of the score. Excluding or masking clipped tiles left
a non-monotone curve; keeping them is monotone if the clipped highlights are
near the clip level, but a highlight far above it can make *any* gradient
metric rise with small defocus. `analyze_terrestrial_focus` therefore reports
the clipped share of the score: `AutofocusController` steps the exposure down
while it is above one half and reports `highlights_clipped` if it stays there;
`confidence` is reduced by it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

#: Block-mean binning factor applied before measuring (noise sigma / 4 for 4x4).
_BIN_FACTOR = 4

#: A binned tile must keep at least this many pixels per side; smaller
#: `tile_size_px` values bin less (down to no binning).
_MIN_BINNED_TILE_PX = 8

#: A tile is moving only if its frame-to-frame change exceeds this multiple of
#: the frame's typical (median-tile) change -- which is the noise-only change
#: on any scene that is not mostly moving.
_MOTION_NOISE_FACTOR = 3.0

#: ...and that change also exceeds this multiple of the tile's own (median-sample)
#: standard deviation, i.e. the content itself was replaced: two independent views of
#: the same texture differ by 2/sqrt(pi) ~ 1.13 x its std on average, while the real
#: mast of diagnostic 402eabde -- jittering by air shimmer, glints flickering on it --
#: reached at most 0.80 (its tiles must stay: they are the focus evidence, and
#: dropping them per sample made the score depend on the sample set, review F1).
#: Measured alternatives (a per-tile correlation test, lower ratios) rejected mast tiles
#: and widened the real-frame sample-set spread to 4.9-7.0 % (this: 4.4 %).
_MOTION_RATIO_THRESHOLD = 1.0

#: The score follows this fraction of the static tiles (the strongest detail),
#: at least `_MIN_DETAIL_TILES` of them.
_TOP_DETAIL_FRACTION = 0.03
_MIN_DETAIL_TILES = 3

#: Expected Sobel gx^2 + gy^2 of white noise per unit pixel variance: 2 x (1+4+1+1+4+1).
_SOBEL_NOISE_GAIN = 24.0

#: A selected tile counts as real detail (for confidence) when its energy is at
#: least this multiple of the frame's noise floor.
_DETAIL_OVER_FLOOR = 2.0

#: A frame is treated as clipped when at least this many pixels sit exactly at
#: its maximum value (a natural scene rarely repeats its exact maximum).
_MIN_CLIPPED_PIXELS = 4

#: Detail-tile count at which aggregate confidence reaches 1.0 -- mirrors
#: star_focus_metric's own `_FULL_CONFIDENCE_STAR_COUNT` convention.
_FULL_CONFIDENCE_TILE_COUNT = 3


@dataclass(frozen=True)
class TerrestrialFocusMeasurement:
    """`sharpness` is the mean Tenengrad energy of the strongest-detail static
    tiles minus the energy of the samples' own sensor noise (0 for a single
    frame) -- `None` if nothing rose above that floor (a flat or entirely
    moving scene). `confidence` falls with the number of selected tiles clearly
    above the floor and with the share of the score carried by clipped tiles.

    `usable_tile_count` is the number of tiles the score follows;
    `rejected_moving_tile_count` the tiles whose content changed between
    samples; `rejected_low_texture_tile_count` the static tiles below the
    strongest-detail selection."""

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


def _bin(image: np.ndarray, factor: int) -> np.ndarray:
    """Block-mean binning, cropping a remainder row/column band."""
    data = np.asarray(image, dtype=np.float64)
    if factor == 1:
        return data
    height = (data.shape[0] // factor) * factor
    width = (data.shape[1] // factor) * factor
    blocks = data[:height, :width].reshape(height // factor, factor, width // factor, factor)
    return np.asarray(blocks.mean(axis=(1, 3)))


def _per_tile(values: np.ndarray, tile: int, reduce: str) -> np.ndarray:
    """A whole-tile grid of one reduction (`mean`/`std`/`max`) over `values`."""
    rows, cols = values.shape[0] // tile, values.shape[1] // tile
    blocks = values[: rows * tile, : cols * tile].reshape(rows, tile, cols, tile)
    if reduce == "std":
        return np.asarray(blocks.std(axis=(1, 3)))
    if reduce == "max":
        return np.asarray(blocks.max(axis=(1, 3)))
    return np.asarray(blocks.mean(axis=(1, 3)))


def _clipped_mask(frame: np.ndarray) -> np.ndarray:
    data = np.asarray(frame)
    top = data.max()
    clipped = np.asarray(data >= top, dtype=bool)
    if int(np.count_nonzero(clipped)) < _MIN_CLIPPED_PIXELS:
        return np.zeros(data.shape, dtype=bool)
    return clipped


def _distinct(frames: Sequence[np.ndarray]) -> list[np.ndarray]:
    """Drop samples identical to an earlier one: a re-read of the same frame carries no
    new information, and as a 'pair' it would fake a zero-noise, zero-motion sample."""
    distinct: list[np.ndarray] = []
    for frame in frames:
        data = np.asarray(frame)
        if not any(
            seen.shape == data.shape and np.array_equal(seen, data) for seen in distinct
        ):
            distinct.append(data)
    return distinct


def _pair_reduce(binned: list[np.ndarray], tile: int, reduce: str) -> np.ndarray:
    """One per-tile statistic of every sample-pair difference, stacked (pairs, rows, cols).

    Every pair, not "each sample vs the first": the result does not depend on the order
    in which the samples arrived (issue #56 review F1)."""
    stats = []
    for first in range(len(binned)):
        for second in range(first + 1, len(binned)):
            difference = binned[second] - binned[first]
            if reduce == "abs_mean":
                stats.append(_per_tile(np.abs(difference), tile, "mean"))
            else:
                stats.append(_per_tile(difference, tile, "std") ** 2)
    return np.stack(stats)


def _moving_tiles(binned: list[np.ndarray], tile: int, clipped: np.ndarray) -> np.ndarray:
    """A tile moves when its largest pairwise change is far above the frame's noise-only
    change AND large against the tile's own content (fully replaced content changes by
    ~1.13 x its std; a static edge jittering by a fraction of a pixel by far less)."""
    change = np.max(_pair_reduce(binned, tile, "abs_mean"), axis=0)
    unclipped = change[~clipped]
    noise_change = float(np.median(unclipped if unclipped.size else change))
    content_std = np.median([_per_tile(sample, tile, "std") for sample in binned], axis=0)
    return np.asarray(
        (change > _MOTION_NOISE_FACTOR * noise_change)
        & (change > _MOTION_RATIO_THRESHOLD * content_std)
    )


def _noise_floor(
    binned: list[np.ndarray], tile: int, clipped: np.ndarray, energy: np.ndarray
) -> float:
    """The Tenengrad energy pure sensor noise contributes to one binned sample.

    Measured from the samples themselves: the median tile variance of a sample-pair
    difference is twice the per-sample noise variance (moving and clipped tiles are a
    minority the median ignores); white noise of variance v gives a Sobel energy of
    `_SOBEL_NOISE_GAIN` x v. Capped by the median tile energy (no tile holds less than
    the noise). A single sample cannot tell noise from texture -> 0."""
    if len(binned) < 2:
        return 0.0
    variances = _pair_reduce(binned, tile, "variance")
    usable = variances[:, ~clipped] if bool(np.any(~clipped)) else variances
    sample_variance = float(np.median(usable)) / 2.0
    return min(_SOBEL_NOISE_GAIN * sample_variance, float(np.median(energy)))


def _clipped_share(selected: np.ndarray, clipped: np.ndarray, floor: float) -> float:
    positive = np.clip(selected - floor, 0.0, None)
    total = float(positive.sum())
    return float(positive[clipped].sum()) / total if total > 0.0 else 0.0


def _confidence(selected: np.ndarray, clipped_share: float, floor: float) -> float:
    detail_count = int(np.count_nonzero(selected >= _DETAIL_OVER_FLOOR * floor))
    base = min(1.0, detail_count / _FULL_CONFIDENCE_TILE_COUNT)
    return base * (1.0 - clipped_share)


@dataclass(frozen=True)
class TerrestrialFocusEvidence:
    """`measure_terrestrial_focus`'s result plus `clipped_share`: the fraction of the score
    carried by tiles with clipped pixels (0 when nothing is clipped) -- what the autofocus
    controller needs to decide on an exposure step-down (issue #56 review F3)."""

    measurement: TerrestrialFocusMeasurement
    clipped_share: float


def _no_evidence(moving: int = 0, low_texture: int = 0) -> TerrestrialFocusEvidence:
    return TerrestrialFocusEvidence(
        TerrestrialFocusMeasurement(
            sharpness=None, confidence=0.0, usable_tile_count=0,
            rejected_moving_tile_count=moving, rejected_low_texture_tile_count=low_texture,
        ),
        clipped_share=0.0,
    )


def analyze_terrestrial_focus(
    frames: Sequence[np.ndarray], *, tile_size_px: int = 64
) -> TerrestrialFocusEvidence:
    """`measure_terrestrial_focus` with the clipped share of its score."""
    factor = max(1, min(_BIN_FACTOR, tile_size_px // _MIN_BINNED_TILE_PX))
    tile = max(1, tile_size_px // factor)
    samples = _distinct(frames)
    binned = [_bin(frame, factor) for frame in samples]
    rows, cols = binned[0].shape[0] // tile, binned[0].shape[1] // tile
    if rows == 0 or cols == 0:
        return _no_evidence()

    clipped = np.zeros((rows, cols), dtype=bool)
    for frame in samples:
        clipped |= _per_tile(_bin(_clipped_mask(frame), factor), tile, "max")[:rows, :cols] > 0

    if len(binned) > 1:
        moving = _moving_tiles(binned, tile, clipped)
    else:
        moving = np.zeros((rows, cols), dtype=bool)

    # Each sample's own energy, averaged: order-independent and shift-invariant (a static
    # edge jittering between samples keeps its energy; averaging the frames would blur it).
    energies = []
    for sample in binned:
        gx, gy = _sobel_gradients(sample)
        energies.append(_per_tile(gx**2 + gy**2, tile, "mean"))
    energy = np.mean(energies, axis=0)
    floor = _noise_floor(binned, tile, clipped, energy)

    static = ~moving
    static_energy = energy[static]
    rejected_moving = int(np.count_nonzero(moving))
    if static_energy.size == 0:
        return _no_evidence(moving=rejected_moving)
    count = min(
        static_energy.size,
        max(_MIN_DETAIL_TILES, round(static_energy.size * _TOP_DETAIL_FRACTION)),
    )
    order = np.argsort(static_energy, kind="stable")[::-1][:count]
    selected = static_energy[order]
    sharpness = float(np.mean(selected)) - floor
    if not np.isfinite(sharpness) or sharpness <= 0.0:
        # Nothing rises above the samples' own noise (or the frames held NaN/inf):
        # no usable focus evidence.
        return _no_evidence(moving=rejected_moving, low_texture=int(static_energy.size))
    clipped_share = _clipped_share(selected, clipped[static][order], floor)
    return TerrestrialFocusEvidence(
        TerrestrialFocusMeasurement(
            sharpness=sharpness,
            confidence=_confidence(selected, clipped_share, floor),
            usable_tile_count=int(count),
            rejected_moving_tile_count=rejected_moving,
            rejected_low_texture_tile_count=int(static_energy.size - count),
        ),
        clipped_share=clipped_share,
    )


def measure_terrestrial_focus(
    frames: Sequence[np.ndarray], *, tile_size_px: int = 64
) -> TerrestrialFocusMeasurement:
    """`frames` are 2D mono analysis planes, all from the SAME focuser
    position (one or more samples -- 2+ distinct ones let moving tiles be
    identified and excluded and the noise floor be measured; a single frame
    skips both, since there is nothing to compare against; identical samples
    count once). The result does not depend on the order of the samples.
    `tile_size_px` is the tile edge in frame (unbinned) pixels."""
    return analyze_terrestrial_focus(frames, tile_size_px=tile_size_px).measurement
