"""Issue #58: group `detect_sources` fragments into one candidate per star blob.

`detect_sources` (shared with other features, unchanged) splits a heavily over-exposed
artificial star -- a flat clipped plateau ~100 px across with a wide halo -- into dozens of
local maxima: on the real frame of diagnostic 4c5677d0 four such stars gave 785 sources.
For focus target acquisition/tracking every blob must be ONE candidate.

Rule (topographic prominence): a source starts a new candidate only if it is a separate peak
-- the connected region around it that stays above `its level - tolerance` reaches no pixel
already claimed by a brighter candidate. Fragments of a plateau or of its halo always reach
the brighter part of their own blob without dropping more than the noise, so they join it;
a second star is separated from its neighbour by a real dip (the saddle between them).
The image is box-smoothed first so the tolerance can be a few noise sigmas of the smoothed
image; `tolerance = max(prominence_sigma * smoothed noise, prominence_fraction * amplitude)`
(the fraction only matters for noise-free synthetic frames).

A frame with at most one source is returned unchanged (one candidate = that source), so a
single well-exposed star behaves exactly as before.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from astrotool_core.target.point_source import PointSource

#: Box size (px) of the smoothing before the prominence test.
_SMOOTH_BOX_PX = 5
#: A separate peak must rise this many smoothed-noise sigmas above the saddle to its
#: brighter neighbour. On the real 4c5677d0 frames every value from 3 to 30 gives exactly the
#: 4 stars (2 leaves halo pieces as extra candidates); the saddle between the two touching
#: stars is 15-20 % of their amplitude below the plateaus, hence the small fraction below.
DEFAULT_PROMINENCE_SIGMA = 8.0
#: ... and at least this share of its own amplitude above the background (noise-free frames).
DEFAULT_PROMINENCE_FRACTION = 0.05
#: Margin (px) around the sources' bounding box in which regions are grown.
_CROP_MARGIN_PX = 32
#: Upper bound of one region (pixels) -- a guard against flooding a whole frame.
_MAX_REGION_PX = 400_000
#: About this many pixels are sampled for the background/noise estimate.
_STAT_SAMPLE_PX = 250_000
_NEIGHBOURS = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))


@dataclass(frozen=True)
class StarCandidate:
    """One star blob. `source` is its brightest detected source (its `fwhm_*`/`donut_like`
    are the blob's); `x`/`y` are the source's own centroid for a one-source blob, else the
    background-subtracted centroid of the blob's bright core. `saturated` is True when any
    source of the blob is clipped."""

    x: float
    y: float
    source: PointSource
    saturated: bool
    member_count: int


def _single(source: PointSource) -> StarCandidate:
    return StarCandidate(source.x, source.y, source, source.saturated, 1)


def _background_and_noise(frame: np.ndarray) -> tuple[float, float]:
    step = max(1, int(np.ceil((frame.size / _STAT_SAMPLE_PX) ** 0.5)))
    sample = np.asarray(frame[::step, ::step], dtype=np.float64)
    background = float(np.median(sample))
    sigma = 1.4826 * float(np.median(np.abs(sample - background)))
    return background, sigma


def _box_smooth(image: np.ndarray, size: int) -> np.ndarray:
    radius = size // 2
    padded = np.pad(image, radius, mode="edge")
    sums = np.pad(np.cumsum(np.cumsum(padded, axis=0), axis=1), ((1, 0), (1, 0)))
    window = 2 * radius + 1
    total = sums[window:, window:] - sums[:-window, window:] - sums[window:, :-window]
    total += sums[:-window, :-window]
    return np.asarray(total / (window * window))


class _Regions:
    """Region growing on the smoothed crop; `owner` holds the candidate index + 1 of every
    pixel already attributed to a candidate."""

    def __init__(self, smoothed: np.ndarray) -> None:
        self.height, self.width = smoothed.shape
        self.values = smoothed.ravel()
        self.owner = np.zeros(smoothed.size, dtype=np.int32)

    def grow(self, seed: int, level: float) -> tuple[np.ndarray, int]:
        """The connected (8-neighbour) region of pixels >= `level` around `seed`, grown
        until it touches an owned pixel. Returns (region, owner index + 1 or 0)."""
        visited = np.zeros(self.values.size, dtype=bool)
        visited[seed] = True
        frontier = np.array([seed], dtype=np.int64)
        parts = [frontier]
        size = 1
        while frontier.size and size <= _MAX_REGION_PX:
            rows, cols = np.divmod(frontier, self.width)
            neighbours = []
            for dy, dx in _NEIGHBOURS:
                ny, nx = rows + dy, cols + dx
                inside = (ny >= 0) & (ny < self.height) & (nx >= 0) & (nx < self.width)
                neighbours.append(ny[inside] * self.width + nx[inside])
            reached = np.unique(np.concatenate(neighbours))
            reached = reached[~visited[reached] & (self.values[reached] >= level)]
            visited[reached] = True
            owners = self.owner[reached]
            owned = owners[owners > 0]
            if owned.size:
                return np.concatenate(parts), int(np.bincount(owned).argmax())
            parts.append(reached)
            size += reached.size
            frontier = reached
        return np.concatenate(parts), 0


def group_star_candidates(
    frame: np.ndarray,
    sources: Sequence[PointSource],
    *,
    prominence_sigma: float = DEFAULT_PROMINENCE_SIGMA,
    prominence_fraction: float = DEFAULT_PROMINENCE_FRACTION,
) -> tuple[StarCandidate, ...]:
    """One `StarCandidate` per star blob of `frame` (see the module docstring). Sources too
    faint to stand out of the smoothed noise never found a candidate of their own."""
    if len(sources) <= 1:
        return tuple(_single(source) for source in sources)

    height, width = frame.shape
    background, sigma = _background_and_noise(frame)
    noise = sigma / _SMOOTH_BOX_PX  # a box of N x N pixels averages N^2 independent samples
    x0 = max(0, int(min(s.x for s in sources)) - _CROP_MARGIN_PX)
    x1 = min(width, int(max(s.x for s in sources)) + _CROP_MARGIN_PX + 1)
    y0 = max(0, int(min(s.y for s in sources)) - _CROP_MARGIN_PX)
    y1 = min(height, int(max(s.y for s in sources)) + _CROP_MARGIN_PX + 1)
    smoothed = _box_smooth(np.asarray(frame[y0:y1, x0:x1], dtype=np.float64), _SMOOTH_BOX_PX)
    regions = _Regions(smoothed)
    crop_width = x1 - x0

    def seed_of(source: PointSource) -> int:
        row = min(max(int(round(source.y)) - y0, 0), y1 - y0 - 1)
        col = min(max(int(round(source.x)) - x0, 0), crop_width - 1)
        return int(row * crop_width + col)

    order = sorted(
        range(len(sources)),
        key=lambda index: (-regions.values[seed_of(sources[index])], -sources[index].peak, index),
    )
    cores: list[tuple[PointSource, float, float]] = []
    members: list[list[PointSource]] = []
    for index in order:
        source = sources[index]
        seed = seed_of(source)
        if regions.owner[seed]:
            members[regions.owner[seed] - 1].append(source)
            continue
        amplitude = float(regions.values[seed]) - background
        if amplitude < prominence_sigma * noise:
            continue  # indistinguishable from the noise: never a candidate of its own
        tolerance = max(prominence_sigma * noise, prominence_fraction * amplitude)
        level = max(float(regions.values[seed]) - tolerance, background + 0.5 * tolerance)
        region, owner = regions.grow(seed, level)
        if owner:
            regions.owner[region] = owner
            members[owner - 1].append(source)
            continue
        regions.owner[region] = len(cores) + 1
        rows, cols = np.divmod(region, crop_width)
        weights = np.maximum(regions.values[region] - background, 0.0)
        if float(weights.sum()) > 0.0:
            cx = float((cols * weights).sum() / weights.sum()) + x0
            cy = float((rows * weights).sum() / weights.sum()) + y0
        else:
            cx, cy = source.x, source.y
        cores.append((source, cx, cy))
        members.append([source])

    candidates: list[StarCandidate] = []
    for (core, cx, cy), group in zip(cores, members, strict=True):
        if len(group) == 1:
            candidates.append(_single(core))
        else:
            saturated = any(member.saturated for member in group)
            candidates.append(StarCandidate(cx, cy, core, saturated, len(group)))
    return tuple(candidates)
