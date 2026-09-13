"""Stage 4 radial diffraction profile — issue #18: measure the radial
intensity distribution of the focused, stacked star image (Stage 3,
issue #17), exposing central peak intensity, radial distance in
pixels, normalized intensity, detectable ring maxima where present,
and background level. "The analysis result shall remain usable without
the UI" -- domain-layer only, Stage 7 (UI) only displays this later.

New package, not `target/`: `astrotool_core.target`'s own docstring
scopes it to "point-source detection and single-target ROI tracking" --
radial/ring diffraction analysis (this stage, and Stage 5's optical
reference model, Stage 6) is a different concern.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astrotool_core.target.detector import detect_sources
from astrotool_core.target.roi_selector import select_target

#: A Nyquist-style "just resolved" floor -- the requirements doc gives
#: no exact number ("insufficiently sampled" is left as an
#: implementation decision, the same allowance Stage 3's own quality
#: formula had). Below this measured FWHM, fine diffraction structure
#: can't be trusted to be real rather than pixel-grid aliasing.
_MIN_FWHM_FOR_SUFFICIENT_SAMPLING = 3.0

#: A candidate ring's local maximum must exceed its preceding local
#: minimum by at least this much (in normalized-intensity units) to
#: count as a genuine detectable ring rather than noise.
_RING_PROMINENCE_THRESHOLD = 0.02


@dataclass(frozen=True)
class RadialProfile:
    radii_px: tuple[float, ...]
    mean_intensity: tuple[float, ...]
    normalized_intensity: tuple[float, ...]


@dataclass(frozen=True)
class RadialProfileResult:
    """`profile`/`center`/`central_peak_intensity`/`background_level`
    are `None` exactly when `sufficient_sampling` is `False` -- `reason`
    explains which case: `"no_star_detected"` or `"insufficient_
    sampling"`. `first_ring_radius_px` is `None` whenever no ring is
    detectable ("where present" -- not itself an error)."""

    profile: RadialProfile | None
    center: tuple[float, float] | None
    central_peak_intensity: float | None
    background_level: float | None
    first_ring_radius_px: float | None
    sufficient_sampling: bool
    reason: str | None


def _estimate_background(image: np.ndarray) -> float:
    """Iterative sigma-clipped median -- same technique as
    `collimation_measurement.estimate_background`, reimplemented here
    since `core` never imports `apps` (import-linter contract)."""
    flat = image.ravel().astype(np.float64)
    for _ in range(5):
        median = float(np.median(flat))
        sigma = float(np.std(flat))
        if sigma == 0.0:
            return median
        clipped = flat[flat < median + 3.0 * sigma]
        if len(clipped) < 10 or len(clipped) == len(flat):
            break
        flat = clipped
    return float(np.median(flat))


def _bin_radial_profile(
    image: np.ndarray, center: tuple[float, float]
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """1px-wide radial bins of Euclidean distance from `center`, out to
    the shortest distance from `center` to any frame edge -- so every
    bin is a *complete* ring, never clipped by the frame boundary (a
    partial-ring bin would bias its own mean low without saying so)."""
    height, width = image.shape
    yy, xx = np.indices((height, width), dtype=np.float64)
    r = np.hypot(xx - center[0], yy - center[1])

    max_r = min(center[0], center[1], width - 1 - center[0], height - 1 - center[1])
    n_bins = int(np.floor(max_r))
    if n_bins < 1:
        return (), ()

    radii: list[float] = []
    means: list[float] = []
    for i in range(n_bins):
        mask = (r >= i) & (r < i + 1)
        if not np.any(mask):
            continue
        radii.append(i + 0.5)
        means.append(float(np.mean(image[mask])))
    return tuple(radii), tuple(means)


def _detect_first_ring(
    radii: tuple[float, ...], normalized: tuple[float, ...]
) -> float | None:
    """The first local maximum after the profile's own first local
    minimum past the central peak, with a minimum prominence over that
    minimum -- "detectable ring maxima where present", not the dark
    null itself."""
    count = len(normalized)
    if count < 5:
        return None

    min_index: int | None = None
    for i in range(1, count - 1):
        if normalized[i] < normalized[i - 1] and normalized[i] < normalized[i + 1]:
            min_index = i
            break
    if min_index is None:
        return None

    for i in range(min_index + 1, count - 1):
        if normalized[i] > normalized[i - 1] and normalized[i] > normalized[i + 1]:
            prominence = normalized[i] - normalized[min_index]
            if prominence > _RING_PROMINENCE_THRESHOLD:
                return radii[i]
            return None
    return None


def compute_radial_profile(
    image: np.ndarray, *, min_fwhm_px: float = _MIN_FWHM_FOR_SUFFICIENT_SAMPLING
) -> RadialProfileResult:
    """`image` is a single, already-stacked 2D mono analysis plane
    (Stage 3's own `StackResult.stacked`) -- the star center is
    re-measured here, not carried over from an earlier stage (Stage 2/3
    precedent: neither reused Stage 1's identity-resolution machinery
    either)."""
    detection = detect_sources(image)
    star = select_target(detection)
    if star is None:
        return RadialProfileResult(
            profile=None, center=None, central_peak_intensity=None, background_level=None,
            first_ring_radius_px=None, sufficient_sampling=False, reason="no_star_detected",
        )

    fwhm = None
    if star.fwhm_x is not None and star.fwhm_y is not None:
        fwhm = (star.fwhm_x + star.fwhm_y) / 2.0
    if fwhm is None or fwhm < min_fwhm_px:
        return RadialProfileResult(
            profile=None, center=None, central_peak_intensity=None, background_level=None,
            first_ring_radius_px=None, sufficient_sampling=False, reason="insufficient_sampling",
        )

    center = (star.x, star.y)
    background = _estimate_background(image)
    radii, means = _bin_radial_profile(image, center)
    if not radii:
        return RadialProfileResult(
            profile=None, center=center, central_peak_intensity=None, background_level=background,
            first_ring_radius_px=None, sufficient_sampling=False, reason="insufficient_sampling",
        )

    denominator = max(star.peak - background, 1e-6)
    normalized = tuple((mean - background) / denominator for mean in means)
    first_ring = _detect_first_ring(radii, normalized)
    profile = RadialProfile(radii_px=radii, mean_intensity=means, normalized_intensity=normalized)
    return RadialProfileResult(
        profile=profile, center=center, central_peak_intensity=float(star.peak),
        background_level=background, first_ring_radius_px=first_ring,
        sufficient_sampling=True, reason=None,
    )
