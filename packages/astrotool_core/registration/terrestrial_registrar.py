"""TerrestrialRegistrar — issue #29's terrestrial cross-camera matcher:
locates one optical train's frame content within another's by matching
image *content* (allowing for rotation, scale, and translation), for
scenes with no sky-coordinate truth source (`star_field_registrar` is the
ASTAP/WCS-backed counterpart for real star fields).

Moved here from `collimation_tool.ui.fov_registration` (issue #29:
cross-camera registration is reusable domain logic with zero Qt/UI
dependency — it never belonged in `apps/` in the first place) and
generalized to the common `CrossCameraRegistrationResult` contract, but
the actual matching algorithm is unchanged from its original, real-
incident-tuned form — see `TERRESTRIAL_TARGET_ACCURACY_PX` and every
docstring section below for the reports that shaped it.

Approach: normalized cross-correlation (NCC), the same technique behind
OpenCV's ``matchTemplate(..., TM_CCOEFF_NORMED)``, implemented here with
plain FFT + a summed-area table so this project doesn't need to add
OpenCV or SciPy as a dependency. NCC is invariant to a linear
brightness/contrast difference between the two images, which matters
here since the two cameras are expected to run at different exposures.
Rotation isn't something NCC alone can search — it only scores
translations — so this rotates a scaled copy of frame A over a grid of
candidate angles (and, since the optical-prior-derived scale ratio may
itself be slightly off, a small grid of candidate scales around it too)
and keeps whichever (scale, angle, position) scores best overall.

Deliberately a one-shot, explicitly-triggered calibration, not something
run on every polled frame — see `collimation_tool.ui.fov_calibrator`.

Issue #29 #7 (spectral/color differences): this module takes already-mono
arrays and never touches raw Bayer/RGB data itself — the green-
overrepresentation risk it warns about is a demosaic-time concern, and
every caller in this project already converts through
`astrotool_core.frames.demosaic` + `rgb_to_luma` (proper per-pixel RGB
reconstruction, then a single photometric luma) before a frame ever
reaches here, so no raw-channel bias reaches this module's own matching.

Issue #29 #9 (OAG): this module's algorithm inherently requires frame A's
content to actually appear somewhere within frame B (a template-in-search
correlation) — the issue explicitly does not require terrestrial mode to
support OAG's adjacent-but-never-overlapping geometry ("Terrestrial
containment/pattern matching does not need to support the OAG geometry
unless a later requirement establishes a useful terrestrial use case").
`StarFieldRegistrar` is the OAG-capable mode.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from astrotool_core.registration.geometry import overlap_polygon, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior, scale_ratio
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)


def _bilinear_sample(
    image: np.ndarray, xs: np.ndarray, ys: np.ndarray, fill_value: float
) -> np.ndarray:
    """Sample *image* at floating-point coordinates (xs, ys), bilinearly
    interpolated; out-of-bounds coordinates get *fill_value*."""
    height, width = image.shape
    x0 = np.floor(xs).astype(np.int64)
    y0 = np.floor(ys).astype(np.int64)
    x1 = x0 + 1
    y1 = y0 + 1
    valid = (x0 >= 0) & (x1 <= width - 1) & (y0 >= 0) & (y1 <= height - 1)
    x0c = np.clip(x0, 0, width - 1)
    x1c = np.clip(x1, 0, width - 1)
    y0c = np.clip(y0, 0, height - 1)
    y1c = np.clip(y1, 0, height - 1)
    wx = xs - x0
    wy = ys - y0
    top = image[y0c, x0c] * (1 - wx) + image[y0c, x1c] * wx
    bottom = image[y1c, x0c] * (1 - wx) + image[y1c, x1c] * wx
    interpolated = top * (1 - wy) + bottom * wy
    return np.where(valid, interpolated, fill_value)


def _resize_bilinear(image: np.ndarray, new_height: int, new_width: int) -> np.ndarray:
    """Resample *image* to (new_height, new_width) via bilinear
    interpolation — a plain-numpy stand-in for cv2.resize/PIL.resize,
    which this project doesn't depend on."""
    height, width = image.shape
    if new_height == height and new_width == width:
        return image.astype(np.float64, copy=True)
    y_src = (np.arange(new_height) + 0.5) * (height / new_height) - 0.5
    x_src = (np.arange(new_width) + 0.5) * (width / new_width) - 0.5
    ys, xs = np.meshgrid(y_src, x_src, indexing="ij")
    return _bilinear_sample(image.astype(np.float64), xs, ys, fill_value=float(image.mean()))


def _rotate_bilinear(image: np.ndarray, angle_deg: float, fill_value: float) -> np.ndarray:
    """Rotate *image* by *angle_deg* about its own center, same shape in
    and out. Forward convention: a point at ``src`` in the input appears
    at ``R(angle_deg) @ src_rel + center`` in the output — matches
    `astrotool_core.registration.geometry.rect_polygon`'s own convention.
    """
    if angle_deg % 360.0 == 0.0:
        return image.astype(np.float64, copy=True)
    height, width = image.shape
    theta = np.deg2rad(angle_deg)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    cy, cx = (height - 1) / 2.0, (width - 1) / 2.0
    ys_out, xs_out = np.meshgrid(np.arange(height), np.arange(width), indexing="ij")
    dx = xs_out - cx
    dy = ys_out - cy
    # Inverse-map each output pixel back to its source coordinate: the
    # inverse of the forward rotation R(angle) is R(-angle).
    src_x = cos_t * dx + sin_t * dy + cx
    src_y = -sin_t * dx + cos_t * dy + cy
    return _bilinear_sample(image.astype(np.float64), src_x, src_y, fill_value)


def _normalized_cross_correlation_surface(search: np.ndarray, template: np.ndarray) -> np.ndarray:
    """NCC score for every position where *template* fits fully within
    *search* — a 'valid'-mode correlation surface, shape
    ``(search_h - template_h + 1, search_w - template_w + 1)``.
    Equivalent to ``cv2.matchTemplate(search, template, TM_CCOEFF_NORMED)``,
    implemented with FFT (for the raw cross term) plus a summed-area table
    (for each window's local mean/variance) so this doesn't need OpenCV.
    """
    h_s, w_s = search.shape
    h_t, w_t = template.shape
    if h_t > h_s or w_t > w_s:
        raise ValueError("template must fit within search on both axes")
    n = h_t * w_t
    template_mean = float(template.mean())
    template_ss = float(np.sum((template - template_mean) ** 2))
    out_h, out_w = h_s - h_t + 1, w_s - w_t + 1
    if template_ss <= 0.0:
        # A perfectly flat template can't be meaningfully correlated —
        # every position is equally "matching" (or not).
        return np.zeros((out_h, out_w), dtype=np.float64)

    fft_h, fft_w = h_s + h_t - 1, w_s + w_t - 1
    search_fft = np.fft.rfft2(search.astype(np.float64), s=(fft_h, fft_w))
    # Correlation, not convolution: flip the template so FFT convolution
    # implements correlation instead.
    template_fft = np.fft.rfft2(template[::-1, ::-1], s=(fft_h, fft_w))
    full = np.fft.irfft2(search_fft * template_fft, s=(fft_h, fft_w))
    raw_cross = full[h_t - 1 : h_t - 1 + out_h, w_t - 1 : w_t - 1 + out_w]

    padded = np.pad(search.astype(np.float64), ((1, 0), (1, 0)))
    integral = np.cumsum(np.cumsum(padded, axis=0), axis=1)
    padded_sq = np.pad(search.astype(np.float64) ** 2, ((1, 0), (1, 0)))
    integral_sq = np.cumsum(np.cumsum(padded_sq, axis=0), axis=1)

    window_sum = (
        integral[h_t:, w_t:]
        - integral[:-h_t, w_t:]
        - integral[h_t:, :-w_t]
        + integral[:-h_t, :-w_t]
    )
    window_sum_sq = (
        integral_sq[h_t:, w_t:]
        - integral_sq[:-h_t, w_t:]
        - integral_sq[h_t:, :-w_t]
        + integral_sq[:-h_t, :-w_t]
    )
    window_ss = window_sum_sq - (window_sum**2) / n  # sum((x-mean)^2) per window

    numerator = raw_cross - template_mean * window_sum
    denominator = np.sqrt(np.clip(window_ss, 0.0, None) * template_ss)
    result: np.ndarray = np.divide(
        numerator, denominator, out=np.zeros_like(numerator), where=denominator > 1e-6
    )
    return result


#: Below this fraction of frame B's own overall standard deviation, a
#: candidate template is treated as carrying no usable signal — see
#: TerrestrialRegistrar.register's "Featureless regions" docstring section.
_DEFAULT_MIN_RELATIVE_CONTRAST = 0.05

#: Below this, a template/window is treated as lacking genuine high-
#: frequency detail regardless of its overall contrast — checked locally,
#: per candidate, not as a single whole-frame average. A real, heavily
#: out-of-focus daytime capture (sky and a blurred landscape, no resolved
#: stars) scored ~0.002, vs. every synthetic starfield test scoring
#: 0.16-0.23 — over 75x lower.
_DEFAULT_MIN_SHARPNESS_RATIO = 0.02

#: How much larger than the matched footprint to grow a window before
#: computing its _sharpness_ratio for the check above — 2.0 (each
#: dimension doubled, ~4x the area) took a real hopeless-pair false
#: reading of 0.074 down to 0.004; 1.0 (the bare matched footprint) did not.
_SHARPNESS_CHECK_PADDING_FACTOR = 4.0

#: Issue #29 #6's own accuracy targets, documented here (not enforced as
#: a hard gate — see the issue's own "Do not promise sub-pixel terrestrial
#: accuracy... may be accepted and documented rather than endlessly
#: tuning") for a caller/diagnostic to compare a real result's own
#: magnitude against.
TERRESTRIAL_DESIRABLE_ACCURACY_PX = 5.0
TERRESTRIAL_ACCEPTABLE_ACCURACY_PX = 50.0
TERRESTRIAL_FALLBACK_ACCURACY_PX = 100.0

#: If a second, sufficiently-distant candidate scores within this
#: fraction of the best candidate's own score, the match is reported as
#: AMBIGUOUS_MATCH rather than trusted outright — issue #29 #5:
#: "repeated patterns create materially ambiguous candidates".
_AMBIGUITY_SCORE_RATIO = 0.92


def _expand_window(
    center_row: float,
    center_col: float,
    height: int,
    width: int,
    factor: float,
    bounds: tuple[int, int],
) -> tuple[int, int, int, int]:
    """A ``(row, col, height, width)`` box `factor` times as large as
    ``height x width`` in each dimension, centered on ``(center_row,
    center_col)`` and clamped to ``bounds`` (the source array's own shape).
    """
    bounds_h, bounds_w = bounds
    padded_h = min(bounds_h, round(height * factor))
    padded_w = min(bounds_w, round(width * factor))
    row = int(min(max(0, round(center_row - padded_h / 2.0)), bounds_h - padded_h))
    col = int(min(max(0, round(center_col - padded_w / 2.0)), bounds_w - padded_w))
    return row, col, padded_h, padded_w


def _sharpness_ratio(image: np.ndarray) -> float:
    """High-frequency (pixel-to-pixel gradient) energy relative to the
    image's own overall variance — a *ratio* to variance, not a raw
    gradient magnitude, so a smooth image with large overall variance
    (e.g. a bright sky next to a dark landscape) doesn't pass on
    brightness contrast alone."""
    variance = float(image.var())
    if variance <= 0.0:
        return 0.0
    grad_x = np.diff(image, axis=1)
    grad_y = np.diff(image, axis=0)
    energy = float(np.mean(grad_x**2)) + float(np.mean(grad_y**2))
    return energy / variance


@dataclass(frozen=True)
class _Candidate:
    center_row: float
    center_col: float
    height_px: float
    width_px: float
    rotation_deg: float
    scale: float
    score: float
    #: The best-scoring position in this *same* (scale, angle)'s own
    #: correlation surface, at least half a template dimension away from
    #: `score`'s own position -- catches a repeated pattern whose second
    #: occurrence best matches at the *same* angle/scale as the first
    #: (see `_is_distinct`'s own docstring for the cross-candidate case
    #: this doesn't cover: a second occurrence whose own best angle
    #: differs from the primary's).
    same_surface_second_peak_score: float


@dataclass(frozen=True)
class _SearchOutcome:
    """Internal search bookkeeping used to classify *why* a search failed
    -- see `TerrestrialRegistrar.register`'s status derivation."""

    best: _Candidate | None
    second_best: _Candidate | None
    any_valid_scale: bool
    any_sharp_candidate: bool


def _score_angle_candidate(
    b_search: np.ndarray,
    resized: np.ndarray,
    fill_value: float,
    frame_b: np.ndarray,
    scale: float,
    scaled_h: int,
    scaled_w: int,
    angle: float,
    downsample: int,
    full_scaled_h: int,
    full_scaled_w: int,
    template_sharp: float,
    min_sharpness_ratio: float,
) -> _Candidate | None:
    """One (scale, angle) hypothesis's own best-scoring position, or None
    if it fails the sharpness guard (see `_DEFAULT_MIN_SHARPNESS_RATIO`)
    -- factored out of `_search`'s own loop purely to keep that loop's
    cyclomatic complexity within this project's C90 gate; no independent
    meaning outside it."""
    rotated = _rotate_bilinear(resized, angle, fill_value)
    surface = _normalized_cross_correlation_surface(b_search, rotated)
    row, col = np.unravel_index(int(np.argmax(surface)), surface.shape)
    score = float(surface[row, col])

    # This same surface's own second-best peak, at least half a template
    # dimension away from the primary one -- see
    # _Candidate.same_surface_second_peak_score's own docstring.
    row_radius = max(1, scaled_h // 2)
    col_radius = max(1, scaled_w // 2)
    masked = surface.copy()
    masked[
        max(0, row - row_radius) : row + row_radius + 1,
        max(0, col - col_radius) : col + col_radius + 1,
    ] = -np.inf
    same_surface_second_peak_score = (
        float(np.max(masked)) if np.any(np.isfinite(masked)) else float("-inf")
    )

    full_row, full_col = row * downsample, col * downsample
    win_row, win_col, win_h, win_w = _expand_window(
        full_row + full_scaled_h / 2.0, full_col + full_scaled_w / 2.0,
        full_scaled_h, full_scaled_w, _SHARPNESS_CHECK_PADDING_FACTOR, frame_b.shape,
    )
    window = frame_b[win_row : win_row + win_h, win_col : win_col + win_w]
    sharp_enough = (
        template_sharp >= min_sharpness_ratio and _sharpness_ratio(window) >= min_sharpness_ratio
    )
    if not sharp_enough:
        return None
    return _Candidate(
        center_row=(row + scaled_h / 2.0) * downsample,
        center_col=(col + scaled_w / 2.0) * downsample,
        height_px=float(scaled_h) * downsample,
        width_px=float(scaled_w) * downsample,
        rotation_deg=angle,
        scale=scale,
        score=score,
        same_surface_second_peak_score=same_surface_second_peak_score,
    )


def _search(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    *,
    approx_scale: float,
    scale_search_fraction: float,
    scale_steps: int,
    angle_step_deg: float,
    angle_range_deg: tuple[float, float],
    min_relative_contrast: float,
    min_sharpness_ratio: float,
    search_downsample: int,
    progress_callback: Callable[[int, int], None] | None,
) -> _SearchOutcome:
    if frame_a.ndim != 2 or frame_b.ndim != 2:
        raise ValueError("frame_a and frame_b must be 2D mono arrays")
    if approx_scale <= 0.0:
        raise ValueError("approx_scale must be positive")

    downsample = max(1, int(search_downsample))
    if downsample > 1:
        a_search = _resize_bilinear(
            frame_a, max(1, frame_a.shape[0] // downsample), max(1, frame_a.shape[1] // downsample)
        )
        b_search = _resize_bilinear(
            frame_b, max(1, frame_b.shape[0] // downsample), max(1, frame_b.shape[1] // downsample)
        )
    else:
        a_search = frame_a
        b_search = frame_b

    a_h, a_w = a_search.shape
    b_h, b_w = b_search.shape
    b_std = float(b_search.std())
    contrast_floor = min_relative_contrast * b_std if b_std > 0.0 else 0.0

    if scale_steps <= 1:
        scale_candidates = [approx_scale]
    else:
        low = approx_scale * (1.0 - scale_search_fraction)
        high = approx_scale * (1.0 + scale_search_fraction)
        scale_candidates = list(np.linspace(low, high, scale_steps))

    angles = np.arange(angle_range_deg[0], angle_range_deg[1], angle_step_deg)

    valid_scales: list[tuple[float, int, int]] = []
    for scale in scale_candidates:
        scaled_h = max(1, round(a_h * scale))
        scaled_w = max(1, round(a_w * scale))
        if scaled_h > b_h or scaled_w > b_w:
            continue
        if float(_resize_bilinear(a_search, scaled_h, scaled_w).std()) < contrast_floor:
            continue
        valid_scales.append((scale, scaled_h, scaled_w))
    total_candidates = len(valid_scales) * len(angles)

    best: _Candidate | None = None
    second_best: _Candidate | None = None
    any_sharp_candidate = False
    completed = 0
    for scale, scaled_h, scaled_w in valid_scales:
        resized = _resize_bilinear(a_search, scaled_h, scaled_w)
        fill_value = float(resized.mean())

        full_scaled_h = max(1, round(frame_a.shape[0] * scale))
        full_scaled_w = max(1, round(frame_a.shape[1] * scale))
        full_resized = _resize_bilinear(frame_a, full_scaled_h, full_scaled_w)
        template_sharp = _sharpness_ratio(full_resized)

        for angle in angles:
            completed += 1
            candidate = _score_angle_candidate(
                b_search, resized, fill_value, frame_b, scale, scaled_h, scaled_w,
                float(angle), downsample, full_scaled_h, full_scaled_w, template_sharp,
                min_sharpness_ratio,
            )
            if progress_callback is not None:
                progress_callback(completed, total_candidates)
            if candidate is None:
                continue
            any_sharp_candidate = True
            if best is None or candidate.score > best.score:
                # Only demote the old best to second_best if it's spatially
                # distinct from the new one -- two angle/scale steps
                # landing on essentially the same real peak shouldn't
                # trigger a false AMBIGUOUS_MATCH.
                if best is not None and _is_distinct(best, candidate):
                    second_best = best
                best = candidate
            elif (second_best is None or candidate.score > second_best.score) and _is_distinct(
                best, candidate
            ):
                second_best = candidate

    return _SearchOutcome(
        best=best, second_best=second_best,
        any_valid_scale=bool(valid_scales), any_sharp_candidate=any_sharp_candidate,
    )


def _is_distinct(a: _Candidate, b: _Candidate) -> bool:
    """True if `a` and `b` are far enough apart to be genuinely different
    candidates, not the same real peak found at two nearby angle/scale
    steps -- half of `a`'s own smaller footprint dimension, a conservative
    (small) distance so real ambiguity isn't missed."""
    distance = ((a.center_row - b.center_row) ** 2 + (a.center_col - b.center_col) ** 2) ** 0.5
    threshold = min(a.height_px, a.width_px) / 2.0
    return bool(distance > threshold)


class TerrestrialRegistrar:
    """Cross-camera registration for terrestrial (non-astronomical)
    scenes — see this module's own docstring. Stateless; safe to share
    across calls."""

    def register(
        self,
        frame_a: np.ndarray,
        frame_b: np.ndarray,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        *,
        scale_search_fraction: float = 0.15,
        scale_steps: int = 3,
        angle_step_deg: float = 5.0,
        angle_range_deg: tuple[float, float] = (-180.0, 180.0),
        min_score: float = 0.3,
        min_relative_contrast: float = _DEFAULT_MIN_RELATIVE_CONTRAST,
        min_sharpness_ratio: float = _DEFAULT_MIN_SHARPNESS_RATIO,
        search_downsample: int = 1,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> CrossCameraRegistrationResult:
        """Find where `frame_a`'s content appears within `frame_b`,
        searching over rotation, a small range of scale around
        `prior_a`/`prior_b`'s own known plate-scale ratio (see
        `optical_prior.scale_ratio`), and translation (translation is
        solved exactly per (scale, angle) hypothesis via FFT correlation,
        not searched).

        Performance: measured on a real rig (ATR585M 3840x2160 vs.
        GPCMOS02000KPA 1920x1080) at ~0.6s per (scale, angle) correlation
        at full resolution — the defaults (72 angles x 3 scales = 216
        candidates) take roughly two real minutes end to end.
        `search_downsample` is the main lever for that — see
        `collimation_tool.ui.fov_calibrator`, which always passes a
        production value. Always call this off the UI thread.

        Featureless regions / out-of-focus frames / repeated patterns:
        see `_DEFAULT_MIN_RELATIVE_CONTRAST`, `_DEFAULT_MIN_SHARPNESS_RATIO`
        and `_AMBIGUITY_SCORE_RATIO`'s own docstrings — three independent,
        real-incident-motivated guards against a confidently-wrong match.

        Returns a `CrossCameraRegistrationResult` whose `status` explains
        exactly which of those guards (if any) rejected the search —
        never a bare `None` (issue #29's "return a non-valid result
        rather than a confident arbitrary transform").
        """
        approx_scale = scale_ratio(prior_a, prior_b)
        try:
            outcome = _search(
                frame_a, frame_b,
                approx_scale=approx_scale, scale_search_fraction=scale_search_fraction,
                scale_steps=scale_steps, angle_step_deg=angle_step_deg,
                angle_range_deg=angle_range_deg, min_relative_contrast=min_relative_contrast,
                min_sharpness_ratio=min_sharpness_ratio, search_downsample=search_downsample,
                progress_callback=progress_callback,
            )
        except ValueError as exc:
            return CrossCameraRegistrationResult(
                method=RegistrationMethod.TERRESTRIAL,
                status=RegistrationStatus.INSUFFICIENT_STRUCTURE,
                diagnostics={"error": str(exc)},
            )

        if not outcome.any_valid_scale or not outcome.any_sharp_candidate:
            # Never even found real signal to search with -- see
            # _DEFAULT_MIN_RELATIVE_CONTRAST/_DEFAULT_MIN_SHARPNESS_RATIO.
            return CrossCameraRegistrationResult(
                method=RegistrationMethod.TERRESTRIAL,
                status=RegistrationStatus.INSUFFICIENT_STRUCTURE,
                diagnostics={
                    "any_valid_scale": outcome.any_valid_scale,
                    "any_sharp_candidate": outcome.any_sharp_candidate,
                },
            )

        best = outcome.best
        assert best is not None  # any_sharp_candidate=True guarantees this
        if best.score < min_score:
            # Real signal existed, searched cleanly, but nothing matched
            # well enough -- the terrestrial equivalent of "no overlap":
            # there's no sky-coordinate truth here to report a *geometric*
            # NO_OVERLAP the way star-field mode can (issue #29 #5).
            return CrossCameraRegistrationResult(
                method=RegistrationMethod.TERRESTRIAL,
                status=RegistrationStatus.NO_VALID_REGISTRATION,
                diagnostics={"best_score": best.score, "min_score": min_score},
            )
        rival_score = max(
            outcome.second_best.score if outcome.second_best is not None else float("-inf"),
            best.same_surface_second_peak_score,
        )
        if rival_score >= best.score * _AMBIGUITY_SCORE_RATIO:
            return CrossCameraRegistrationResult(
                method=RegistrationMethod.TERRESTRIAL,
                status=RegistrationStatus.AMBIGUOUS_MATCH,
                diagnostics={"best_score": best.score, "rival_score": rival_score},
            )

        polygon_a_in_b = rect_polygon(
            best.width_px, best.height_px,
            center=(best.center_col, best.center_row), rotation_deg=best.rotation_deg,
        )
        frame_b_rect = rect_polygon(
            frame_b.shape[1], frame_b.shape[0],
            center=(frame_b.shape[1] / 2.0, frame_b.shape[0] / 2.0),
        )
        overlap = overlap_polygon(polygon_a_in_b, frame_b_rect)
        status = (
            RegistrationStatus.OK_OVERLAP if overlap else RegistrationStatus.NO_VALID_REGISTRATION
        )
        return CrossCameraRegistrationResult(
            method=RegistrationMethod.TERRESTRIAL,
            status=status,
            rotation_deg=best.rotation_deg,
            scale=best.scale,
            polygon_a_in_b=polygon_a_in_b,
            overlap_polygon=overlap,
            confidence=best.score,
            diagnostics={"prior_a": prior_a.name, "prior_b": prior_b.name},
        )
