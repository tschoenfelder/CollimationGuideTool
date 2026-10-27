"""Issue #58: find artificial-star candidates -- one per star blob -- and measure their size.

Why not `detect_sources` (shared, unchanged for its other callers): on large, bright artificial
stars it fails in three ways seen on diagnostic 4c5677d0 and in review -- (1) with more than
25000 bright pixels it splits every clipped plateau and its halo into dozens of local maxima
(785 sources for 4 stars); (2) with fewer it silently drops any component larger than 5000 px,
so one big defocused or clipped star is not found at all; (3) its width is a moment FWHM in a
37x37 px window, which saturates at about 24 px, so a sweep can never see a big star shrink.
It also takes ~10 s per 3840x2160 frame.

Here (all numpy, ~0.2 s per 3840x2160 frame on the dev PC):

1. The frame is binned `binning` x `binning` (to <= ~600k pixels) and box-smoothed (3x3);
   background and noise are the median and MAD sigma of that smoothed frame (measured, so
   correlated noise and background structure count too).
2. Segmentation: pixels above
   `background + max(5 smoothed sigma, 0.1 % of the brightest amplitude)` are star pixels.
3. One candidate per blob (topographic prominence): local maxima are taken brightest first; one
   starts its own candidate only if the region around it that stays above
   `its level - max(9 smoothed sigma, 5 % of its amplitude)` reaches no brighter candidate.
   Plateau and halo pieces join their blob; two touching stars stay separate (their saddle is
   a real dip). The remaining star pixels go to the candidate they connect to first, so two
   touching stars split their halos at the saddle.
4. Size: the half-flux diameter (HFD) of the candidate's full-resolution pixels after
   subtracting the local background (median of a ring just outside the blob) -- the diameter
   of the circle around the blob's centre that holds half the flux (the centre: centroid of
   the blob's bright core, >= half its peak -- the middle of a donut's ring or of a plateau).
   Unlike a moment FWHM in a fixed window it keeps growing with the star (3 to 150 px in the
   tests, also clipped and for a defocused donut) while the star stands well above the noise.
   `saturated` = any pixel of the blob at >= 98 % of full scale (the same rule as
   `detect_sources`); a clipped star has no trustworthy size.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Box size (binned px) of the smoothing before segmentation.
_SMOOTH_BOX = 3
#: Star pixels: above background + this many smoothed-noise sigmas ...
_DETECTION_SIGMA = 5.0
#: ... and at least this share of the brightest amplitude (noise-free synthetic frames).
_DETECTION_FRACTION = 1.0e-3
#: A separate peak must rise this many smoothed-noise sigmas above the saddle to its brighter
#: neighbour ... (on the real 4c5677d0 frames every value from 6 to 14 gives exactly the 4 stars
#: on all three frames: below, halo pieces become candidates; above, the touching pair merges)
DEFAULT_PROMINENCE_SIGMA = 9.0
#: ... and at least this share of its own amplitude (the saddle between the two touching
#: 4c5677d0 stars is 15-20 % of their amplitude below the plateaus).
DEFAULT_PROMINENCE_FRACTION = 0.05
#: `saturated` threshold as a share of full scale (`detect_sources` uses the same 0.98).
SATURATION_FRACTION = 0.98
#: The binned frame has at most about this many pixels.
_MAX_BINNED_PX = 600_000
#: About this many pixels are sampled for the background/noise estimate.
_STAT_SAMPLE_PX = 250_000
#: Width (binned px) of the ring outside a blob whose median is its local background.
_RING_CELLS = 3
_NEIGHBOURS = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1))


@dataclass(frozen=True)
class StarCandidate:
    """One star blob in full-frame pixel coordinates. `x`/`y`: centroid of the blob's bright
    core (pixels >= half its peak above the local background); a touching neighbour's halo
    can still pull it by a few px. `hfd_px`: half-flux diameter of the blob's pixels, `None`
    when they hold no positive flux; it is cut at the blob's border (the detection threshold,
    and the saddle to a touching neighbour), so a star barely above the noise or crowded by a
    neighbour reads smaller than its true size. `radius_px`: radius of a disc with the
    blob's area. `saturated`: a pixel of
    the blob is at >= 98 % of full scale. `touches_edge`: the blob reaches the frame border.
    `truncated`: it reaches the border of the analysed crop inside the frame (measure again on
    a larger area)."""

    x: float
    y: float
    hfd_px: float | None
    radius_px: float
    saturated: bool
    touches_edge: bool
    truncated: bool = False


@dataclass(frozen=True)
class FrameStatistics:
    """Whole-frame statistics at one `binning`: `background` (median) and `noise` (MAD sigma)
    of the binned, box-smoothed frame -- measured, so correlated noise and background
    structure count too -- and `full_scale` as `detect_sources` infers it from the frame's
    maximum (65535 above 4096, 4095 above 255, else 255)."""

    background: float
    noise: float
    full_scale: float
    binning: int


def _bin(image: np.ndarray, b: int) -> np.ndarray:
    height, width = image.shape[0] // b, image.shape[1] // b
    return np.asarray(
        image[: height * b, : width * b].reshape(height, b, width, b).mean(axis=(1, 3))
    )


def frame_statistics(frame: np.ndarray, binning: int | None = None) -> FrameStatistics:
    b = binning or binning_for(frame.shape)
    smoothed = _box_smooth(_bin(np.asarray(frame, dtype=np.float64), b), _SMOOTH_BOX)
    step = max(1, int(np.ceil((smoothed.size / _STAT_SAMPLE_PX) ** 0.5)))
    sample = smoothed[::step, ::step]
    background = float(np.median(sample))
    noise = 1.4826 * float(np.median(np.abs(sample - background)))
    peak = float(np.max(frame))
    full_scale = 65535.0 if peak > 4096 else 4095.0 if peak > 255 else 255.0
    return FrameStatistics(background, noise, full_scale, b)


def binning_for(shape: tuple[int, int]) -> int:
    """The binning that keeps a frame of `shape` at <= ~600k binned pixels."""
    return max(1, int(np.ceil(np.sqrt(shape[0] * shape[1] / _MAX_BINNED_PX))))


def is_clipped(frame: np.ndarray, full_scale: float, *, min_pixels: int = 4) -> bool:
    """At least `min_pixels` pixels at >= 98 % of `full_scale`."""
    limit = SATURATION_FRACTION * full_scale
    return int(np.count_nonzero(np.asarray(frame) >= limit)) >= min_pixels


def _box_smooth(image: np.ndarray, size: int) -> np.ndarray:
    radius = size // 2
    padded = np.pad(image, radius, mode="edge")
    sums = np.pad(np.cumsum(np.cumsum(padded, axis=0), axis=1), ((1, 0), (1, 0)))
    window = 2 * radius + 1
    total = sums[window:, window:] - sums[:-window, window:] - sums[window:, :-window]
    total += sums[:-window, :-window]
    return np.asarray(total / (window * window))


def _neighbours(flat: np.ndarray, height: int, width: int) -> np.ndarray:
    rows, cols = np.divmod(flat, width)
    parts = []
    for dy, dx in _NEIGHBOURS:
        ny, nx = rows + dy, cols + dx
        inside = (ny >= 0) & (ny < height) & (nx >= 0) & (nx < width)
        parts.append(ny[inside] * width + nx[inside])
    return np.unique(np.concatenate(parts)) if parts else flat


class _Segmentation:
    """Prominence grouping on the smoothed, binned image; `owner` = candidate index + 1."""

    def __init__(self, smoothed: np.ndarray, mask: np.ndarray) -> None:
        self.height, self.width = smoothed.shape
        self.values = smoothed.ravel()
        self.mask = mask.ravel()
        self.owner = np.zeros(self.values.size, dtype=np.int32)
        #: Owner labels for the contact test: the candidates' own regions plus the regions
        #: already flooded from seeds that joined them (speed: a later seed stops as soon as
        #: it reaches one). Only `owner` (the candidates' own regions) seeds the final fill.
        self.explored = np.zeros(self.values.size, dtype=np.int32)

    def grow(self, seed: int, level: float) -> tuple[np.ndarray, int]:
        """The connected region of pixels >= `level` around `seed`, grown until it touches an
        explored pixel. Returns (region, owner index + 1 or 0)."""
        visited = np.zeros(self.values.size, dtype=bool)
        visited[seed] = True
        frontier = np.array([seed], dtype=np.int64)
        parts = [frontier]
        while frontier.size:
            reached = _neighbours(frontier, self.height, self.width)
            reached = reached[~visited[reached] & (self.values[reached] >= level)]
            visited[reached] = True
            owners = self.explored[reached]
            owned = owners[owners > 0]
            if owned.size:
                return np.concatenate(parts), int(np.bincount(owned).argmax())
            parts.append(reached)
            frontier = reached
        return np.concatenate(parts), 0

    def fill(self) -> None:
        """Give every unowned star pixel to the candidate it connects to first (multi-source
        flood inside the star pixels)."""
        frontier = np.flatnonzero(self.owner)
        while frontier.size:
            rows, cols = np.divmod(frontier, self.width)
            labels = self.owner[frontier]
            new: list[np.ndarray] = []
            for dy, dx in _NEIGHBOURS:
                ny, nx = rows + dy, cols + dx
                inside = (ny >= 0) & (ny < self.height) & (nx >= 0) & (nx < self.width)
                target = ny[inside] * self.width + nx[inside]
                free = self.mask[target] & (self.owner[target] == 0)
                self.owner[target[free]] = labels[inside][free]
                new.append(target[free])
            frontier = np.unique(np.concatenate(new))


def find_star_candidates(
    frame: np.ndarray,
    *,
    stats: FrameStatistics | None = None,
    binning: int | None = None,
    origin: tuple[int, int] = (0, 0),
    frame_shape: tuple[int, int] | None = None,
    prominence_sigma: float = DEFAULT_PROMINENCE_SIGMA,
    prominence_fraction: float = DEFAULT_PROMINENCE_FRACTION,
) -> tuple[StarCandidate, ...]:
    """One `StarCandidate` per star blob of `frame` (see the module docstring). `frame` may be
    a crop: `origin` = its (x0, y0) in the full frame, `frame_shape` = the full frame's shape,
    `stats` = the full frame's statistics (a crop's own median is the star)."""
    image = np.asarray(frame, dtype=np.float64)
    b = binning or (stats.binning if stats is not None else binning_for(image.shape))
    if stats is None or stats.binning != b:
        stats = frame_statistics(image, b)
    full_height, full_width = frame_shape or image.shape
    height, width = image.shape[0] // b, image.shape[1] // b
    if height < 3 or width < 3:
        return ()
    blocks = image[: height * b, : width * b].reshape(height, b, width, b)
    binned = blocks.mean(axis=(1, 3))
    binned_max = blocks.max(axis=(1, 3))
    smoothed = _box_smooth(binned, _SMOOTH_BOX)
    noise = stats.noise
    brightest = float(smoothed.max()) - stats.background
    if brightest <= 0.0:
        return ()
    threshold = stats.background + max(_DETECTION_SIGMA * noise, _DETECTION_FRACTION * brightest)
    mask = smoothed > threshold
    if not mask.any():
        return ()

    padded = np.pad(smoothed, 1, mode="constant", constant_values=-np.inf)
    peaks = mask.copy()
    for dy, dx in _NEIGHBOURS:
        peaks &= smoothed >= padded[1 + dy : 1 + dy + height, 1 + dx : 1 + dx + width]
    seeds = np.flatnonzero(peaks)
    seg = _Segmentation(smoothed, mask)
    seeds = seeds[np.argsort(-seg.values[seeds], kind="stable")]
    cores = 0
    for seed in seeds:
        if seg.explored[seed]:
            continue
        amplitude = float(seg.values[seed]) - stats.background
        if amplitude < prominence_sigma * noise:
            continue  # indistinguishable from the noise
        tolerance = max(prominence_sigma * noise, prominence_fraction * amplitude)
        level = max(float(seg.values[seed]) - tolerance, threshold)
        region, owner = seg.grow(int(seed), level)
        if owner:
            # Not a separate peak. Its flooded region is not given to that candidate (near a
            # saddle it spans both neighbours; the final fill splits them at the saddle).
            seg.explored[region] = owner
            continue
        cores += 1
        seg.owner[region] = cores
        seg.explored[region] = cores
    if cores == 0:
        return ()
    seg.fill()
    labels = seg.owner.reshape(height, width)
    return tuple(
        _describe(
            image,
            binned,
            binned_max,
            labels,
            label,
            b,
            stats,
            origin,
            (full_height, full_width),
        )
        for label in range(1, cores + 1)
    )


def _describe(
    image: np.ndarray,
    binned: np.ndarray,
    binned_max: np.ndarray,
    owner: np.ndarray,
    label: int,
    b: int,
    stats: FrameStatistics,
    origin: tuple[int, int],
    full_shape: tuple[int, int],
) -> StarCandidate:
    height, width = owner.shape
    region = owner == label
    rows, cols = np.nonzero(region)
    # Local background: median of a ring of non-star cells just outside the blob.
    grown = region.copy()
    for _ in range(_RING_CELLS):
        shifted = grown.copy()
        shifted[1:, :] |= grown[:-1, :]
        shifted[:-1, :] |= grown[1:, :]
        shifted[:, 1:] |= grown[:, :-1]
        shifted[:, :-1] |= grown[:, 1:]
        grown = shifted
    ring = grown & (owner == 0)
    background = float(np.median(binned[ring])) if ring.any() else stats.background

    offsets = np.arange(b)
    pixel_rows = (rows[:, None] * b + offsets[None, :])[:, :, None]
    pixel_cols = (cols[:, None] * b + offsets[None, :])[:, None, :]
    ys = np.broadcast_to(pixel_rows, (rows.size, b, b)).ravel().astype(np.float64)
    xs = np.broadcast_to(pixel_cols, (rows.size, b, b)).ravel().astype(np.float64)
    flux = image[ys.astype(np.int64), xs.astype(np.int64)] - background
    # Centroid of the bright core (>= half the blob's peak above the local background): the
    # centre of a donut's ring as of a plateau, and not pulled by a touching neighbour's halo.
    peak = float(flux.max()) if flux.size else 0.0
    core = np.where(flux >= 0.5 * peak, flux, 0.0)
    x0, y0 = origin
    if peak > 0.0:
        cx = float((xs * core).sum() / core.sum())
        cy = float((ys * core).sum() / core.sum())
    else:
        cx, cy = float(xs.mean()), float(ys.mean())
    hfd = _half_flux_diameter(xs - cx, ys - cy, flux)

    on_border = bool(
        rows.min() == 0 or cols.min() == 0 or rows.max() == height - 1 or cols.max() == width - 1
    )
    reaches_frame_edge = bool(
        (rows.min() == 0 and y0 == 0)
        or (cols.min() == 0 and x0 == 0)
        or ((rows.max() + 1) * b + y0 >= full_shape[0] - b)
        or ((cols.max() + 1) * b + x0 >= full_shape[1] - b)
    )
    return StarCandidate(
        x=cx + x0,
        y=cy + y0,
        hfd_px=hfd,
        radius_px=float(np.sqrt(rows.size / np.pi)) * b,
        saturated=bool(binned_max[region].max() >= SATURATION_FRACTION * stats.full_scale),
        touches_edge=reaches_frame_edge,
        truncated=on_border and not reaches_frame_edge,
    )


def _half_flux_diameter(dx: np.ndarray, dy: np.ndarray, flux: np.ndarray) -> float | None:
    total = float(flux.sum())
    if total <= 0.0:
        return None
    radius = np.hypot(dx, dy)
    order = np.argsort(radius, kind="stable")
    cumulative = np.cumsum(flux[order])
    index = int(np.searchsorted(cumulative, 0.5 * total))
    index = min(index, radius.size - 1)
    sorted_radius = radius[order]
    if index == 0:
        return float(2.0 * sorted_radius[0])
    # Linear interpolation between the two pixels straddling half the flux.
    below, above = float(cumulative[index - 1]), float(cumulative[index])
    share = 0.0 if above == below else (0.5 * total - below) / (above - below)
    r_half = sorted_radius[index - 1] + share * (sorted_radius[index] - sorted_radius[index - 1])
    return float(2.0 * r_half)
