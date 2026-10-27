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
#: Review N1: a candidate needs at least this many full-resolution pixels above
#: `_SUPPORT_SIGMA` pixel-noise sigmas (`detect_sources`' MIN_STAR_SUPPORT_PIXELS is 5 too) ...
MIN_SUPPORT_PIXELS = 5
_SUPPORT_SIGMA = 5.0
#: ... and a half-flux diameter of at least this (a hot 2x2 cluster has ~1.4 px).
MIN_HFD_PX = 1.5
#: A single pixel brighter than `_HOT_PIXEL_FACTOR` x its brightest neighbour (above the
#: background, + 5 pixel-noise sigmas) is a hot/warm pixel and is replaced by that neighbour.
#: A real star's peak is at most ~2x its neighbours even at FWHM 2 px.
_HOT_PIXEL_FACTOR = 3.0
#: A frame counts as clipped from this many clipped pixels that each have >= 4 clipped
#: neighbours (the inside of a clipped blob -- hot pixels and 2x2 clusters never count).
CLIPPED_FRAME_MIN_PX = 25
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
    """Whole-frame statistics at one `binning`, after hot pixels are removed:
    `background` (median) and `noise` (MAD sigma) of the binned, box-smoothed frame --
    measured, so correlated noise and background structure count too; `brightest`: the
    frame's brightest smoothed amplitude above `background` (review N2: a crop is segmented
    with the whole frame's threshold, so `acquire()` and `measure()` measure on one scale);
    `pixel_background` / `pixel_noise`: median and MAD sigma of single pixels; `full_scale`:
    the camera's (`2**bit_depth - 1`) when given, else inferred like `detect_sources` from
    the frame's maximum (65535 above 4096, 4095 above 255, else 255)."""

    background: float
    noise: float
    full_scale: float
    binning: int
    brightest: float = 0.0
    pixel_background: float = 0.0
    pixel_noise: float = 0.0


def _bin(image: np.ndarray, b: int) -> np.ndarray:
    height, width = image.shape[0] // b, image.shape[1] // b
    return np.asarray(
        image[: height * b, : width * b].reshape(height, b, width, b).mean(axis=(1, 3))
    )


def _pixel_statistics(frame: np.ndarray) -> tuple[float, float]:
    step = max(1, int(np.ceil((frame.size / _STAT_SAMPLE_PX) ** 0.5)))
    sample = np.asarray(frame[::step, ::step], dtype=np.float64)
    background = float(np.median(sample))
    return background, 1.4826 * float(np.median(np.abs(sample - background)))


def inferred_full_scale(frame: np.ndarray) -> float:
    """`detect_sources`' rule from the frame's maximum -- only a fallback for an unknown
    camera bit depth (a dim 16-bit frame below 4096 would read as 12-bit)."""
    peak = float(np.max(frame))
    return 65535.0 if peak > 4096 else 4095.0 if peak > 255 else 255.0


def remove_hot_pixels(frame: np.ndarray, background: float, pixel_noise: float) -> np.ndarray:
    """Review N1: `frame` (as float64) with every single hot/warm pixel -- brighter above
    `background` than `_HOT_PIXEL_FACTOR` x its brightest 8-neighbour + 5 pixel-noise
    sigmas -- replaced by that neighbour. Returns `frame` itself (as float64, not a copy)
    when there is none; never modifies the caller's array."""
    image = np.asarray(frame, dtype=np.float64)
    height, width = image.shape
    rows, cols = np.nonzero(image[1:-1, 1:-1] > background + _SUPPORT_SIGMA * pixel_noise)
    if rows.size == 0:
        return image
    rows += 1
    cols += 1
    neighbour_max = np.full(rows.size, -np.inf)
    for dy, dx in _NEIGHBOURS:
        neighbour_max = np.maximum(neighbour_max, image[rows + dy, cols + dx])
    amplitude = image[rows, cols] - background
    hot = amplitude > _HOT_PIXEL_FACTOR * np.maximum(neighbour_max - background, 0.0) + (
        _SUPPORT_SIGMA * pixel_noise
    )
    if not hot.any():
        return image
    cleaned = image.copy() if image is frame else image
    cleaned[rows[hot], cols[hot]] = neighbour_max[hot]
    return cleaned


def frame_statistics(
    frame: np.ndarray, binning: int | None = None, *, full_scale: float | None = None
) -> FrameStatistics:
    b = binning or binning_for(frame.shape)
    pixel_background, pixel_noise = _pixel_statistics(frame)
    cleaned = remove_hot_pixels(frame, pixel_background, pixel_noise)
    smoothed = _box_smooth(_bin(cleaned, b), _SMOOTH_BOX)
    step = max(1, int(np.ceil((smoothed.size / _STAT_SAMPLE_PX) ** 0.5)))
    sample = smoothed[::step, ::step]
    background = float(np.median(sample))
    noise = 1.4826 * float(np.median(np.abs(sample - background)))
    return FrameStatistics(
        background=background,
        noise=noise,
        full_scale=full_scale if full_scale is not None else inferred_full_scale(frame),
        binning=b,
        brightest=float(smoothed.max()) - background,
        pixel_background=pixel_background,
        pixel_noise=pixel_noise,
    )


def binning_for(shape: tuple[int, int]) -> int:
    """The binning that keeps a frame of `shape` at <= ~600k binned pixels."""
    return max(1, int(np.ceil(np.sqrt(shape[0] * shape[1] / _MAX_BINNED_PX))))


def _clipped_inside(clipped: np.ndarray) -> np.ndarray:
    """Clipped pixels with >= 4 clipped 8-neighbours (the inside of a clipped blob)."""
    padded = np.pad(clipped, 1).astype(np.int8)
    height, width = clipped.shape
    count = np.zeros(clipped.shape, dtype=np.int8)
    for dy, dx in _NEIGHBOURS:
        count += padded[1 + dy : 1 + dy + height, 1 + dx : 1 + dx + width]
    return np.asarray(clipped & (count >= 4))


def is_clipped(
    frame: np.ndarray, full_scale: float, *, min_pixels: int = CLIPPED_FRAME_MIN_PX
) -> bool:
    """At least `min_pixels` pixels at >= 98 % of `full_scale` inside clipped blobs (review
    N1: scattered hot pixels and small hot clusters never make a frame clipped)."""
    clipped = np.asarray(frame) >= SATURATION_FRACTION * full_scale
    if int(np.count_nonzero(clipped)) < min_pixels:
        return False
    rows, cols = np.nonzero(clipped)
    y0, y1 = max(0, rows.min() - 1), rows.max() + 2
    x0, x1 = max(0, cols.min() - 1), cols.max() + 2
    return int(np.count_nonzero(_clipped_inside(clipped[y0:y1, x0:x1]))) >= min_pixels


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
    b = binning or (stats.binning if stats is not None else binning_for(frame.shape))
    if stats is None or stats.binning != b:
        stats = frame_statistics(frame, b)
    image = remove_hot_pixels(frame, stats.pixel_background, stats.pixel_noise)
    full_height, full_width = frame_shape or image.shape
    height, width = image.shape[0] // b, image.shape[1] // b
    if height < 3 or width < 3:
        return ()
    blocks = image[: height * b, : width * b].reshape(height, b, width, b)
    binned = blocks.mean(axis=(1, 3))
    binned_max = blocks.max(axis=(1, 3))
    smoothed = _box_smooth(binned, _SMOOTH_BOX)
    noise = stats.noise
    # Review N2: the WHOLE frame's brightest amplitude, also for a crop.
    brightest = stats.brightest
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
    described = (
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
    return tuple(candidate for candidate in described if candidate is not None)


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
) -> StarCandidate | None:
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
    # Review N1: a star covers several pixels clearly above the noise; a hot cluster does not.
    support = int(
        np.count_nonzero(flux > max(_SUPPORT_SIGMA * stats.pixel_noise, _DETECTION_FRACTION * peak))
    )
    if support < MIN_SUPPORT_PIXELS or (hfd is not None and hfd < MIN_HFD_PX):
        return None

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
    """Twice the radius holding half the flux. Each pixel's flux is spread evenly over the
    radii [r - 0.5, r + 0.5] it covers (review L2: counting it at its centre radius alone made
    the value jump in steps for small, pixel-centred stars), so the enclosed flux is a
    continuous function of the radius; its half point is found by bisection."""
    total = float(flux.sum())
    if total <= 0.0:
        return None
    radius = np.hypot(dx, dy)
    inner = np.maximum(radius - 0.5, 0.0)
    span = radius + 0.5 - inner
    low, high = 0.0, float(radius.max()) + 0.5
    for _ in range(40):
        middle = 0.5 * (low + high)
        enclosed = float((flux * np.clip((middle - inner) / span, 0.0, 1.0)).sum())
        if enclosed < 0.5 * total:
            low = middle
        else:
            high = middle
    return float(low + high)
