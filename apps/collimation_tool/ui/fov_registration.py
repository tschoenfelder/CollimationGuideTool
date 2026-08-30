"""Locates the main camera's actual footprint within a guide-camera frame
by matching image *content*, allowing for rotation, scale, and
translation — the real alignment `fov_overlay.compute_fov_overlay_rect`
never had: that function only knows each optical train's plate scale
from config, so it always draws a centered, unrotated rectangle
regardless of how the two cameras are actually mounted relative to each
other (confirmed: "seems just placed, not really reflecting the main
camera FOV").

Approach: normalized cross-correlation (NCC), the same technique behind
OpenCV's ``matchTemplate(..., TM_CCOEFF_NORMED)``, implemented here with
plain FFT + a summed-area table so this project doesn't need to add
OpenCV or SciPy as a dependency. NCC is invariant to a linear
brightness/contrast difference between the two images, which matters
here since the two cameras are expected to run at different exposures.
Rotation isn't something NCC alone can search — it only scores
translations — so this rotates a scaled copy of the main image over a
grid of candidate angles (and, since the config-derived plate-scale
ratio may itself be slightly off, a small grid of candidate scales
around it too) and keeps whichever (scale, angle, position) scores best
overall.

Deliberately a one-shot, explicitly-triggered calibration, not something
run on every polled frame: a full search (the default 72 angles x 3
scales, each an FFT-based correlation against the full guide frame —
measured at ~0.6s per candidate on a real ATR585M/GPCMOS02000KPA rig,
so on the order of two real minutes end to end) is far too slow for a
live 100ms poll tick, and the two scopes' relative mounting doesn't
change frame to frame — only when the rig is physically adjusted. See
`MainWindow`'s "Calibrate FOV" action and `FovCalibrator` (runs this off
the UI thread).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class FovRegistrationResult:
    """Where and how the main camera's frame content was found within the
    guide frame, in guide-frame pixel coordinates.

    ``rotation_deg`` is how far the main frame's content is rotated
    *within* the guide frame (positive = the standard image-space forward
    rotation used throughout this module — see ``registration_corners``
    for turning this into drawable rectangle corners). ``scale`` is guide
    pixels per main pixel. ``score`` is the normalized cross-correlation
    at the best match, roughly in (0, 1] for a real match — see
    ``min_score``.
    """

    center_x_px: float
    center_y_px: float
    width_px: float
    height_px: float
    rotation_deg: float
    scale: float
    score: float


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
    and out (corners introduced by the rotation are filled with
    *fill_value*, chosen by the caller to be the image's own mean so they
    contribute near-zero to a subsequent cross-correlation).

    Forward convention: a point at ``src`` in the input appears at
    ``R(angle_deg) @ src_rel + center`` in the output, where ``R`` is the
    standard rotation matrix ``[[cos,-sin],[sin,cos]]`` — see
    ``registration_corners``, which relies on this exact convention to
    turn a match back into rectangle corners.
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


#: Below this fraction of the guide frame's own overall standard
#: deviation, a candidate template is treated as carrying no usable
#: signal — see register_main_frame_in_guide_frame's "Featureless
#: regions" docstring section.
_DEFAULT_MIN_RELATIVE_CONTRAST = 0.05

#: Below this, an image is treated as lacking genuine star-like
#: high-frequency detail regardless of its overall contrast — see
#: register_main_frame_in_guide_frame's "Out-of-focus/low-detail
#: images" docstring section. Every synthetic starfield this module's
#: own test suite uses (several sizes/star counts/seeds) scores
#: 0.16-0.23; a real, heavily out-of-focus daytime capture (sky and a
#: blurred landscape, no resolved stars — see the incident this was
#: added for) scored ~0.002, over 75x lower. 0.02 leaves a wide margin
#: on both sides.
_DEFAULT_MIN_SHARPNESS_RATIO = 0.02


def _sharpness_ratio(image: np.ndarray) -> float:
    """High-frequency (pixel-to-pixel gradient) energy relative to the
    image's own overall variance — see _DEFAULT_MIN_SHARPNESS_RATIO.

    Deliberately a *ratio* to variance, not a raw gradient magnitude:
    a smooth image can still have large overall variance (e.g. a bright
    sky next to a dark landscape) without having any genuine fine
    detail to register against, which a raw contrast/std check alone
    (see min_relative_contrast) cannot distinguish from real texture —
    confirmed on the real incident this was added for: the problem
    guide frame's own local window at the (wrong) best-matching position
    had plenty of standard deviation, comfortably above the contrast
    floor, but almost all of it came from a smooth brightness gradient
    rather than any real structure.
    """
    variance = float(image.var())
    if variance <= 0.0:
        return 0.0
    grad_x = np.diff(image, axis=1)
    grad_y = np.diff(image, axis=0)
    energy = float(np.mean(grad_x**2)) + float(np.mean(grad_y**2))
    return energy / variance


def register_main_frame_in_guide_frame(
    main_mono: np.ndarray,
    guide_mono: np.ndarray,
    *,
    approx_scale: float,
    scale_search_fraction: float = 0.15,
    scale_steps: int = 3,
    angle_step_deg: float = 5.0,
    angle_range_deg: tuple[float, float] = (-180.0, 180.0),
    min_score: float = 0.3,
    min_relative_contrast: float = _DEFAULT_MIN_RELATIVE_CONTRAST,
    min_sharpness_ratio: float = _DEFAULT_MIN_SHARPNESS_RATIO,
    search_downsample: int = 1,
    progress_callback: Callable[[int, int], None] | None = None,
) -> FovRegistrationResult | None:
    """Find where ``main_mono``'s content appears within ``guide_mono``,
    searching over rotation, a small range of scale around
    ``approx_scale``, and translation (translation is solved exactly per
    (scale, angle) hypothesis via FFT correlation, not searched).

    ``approx_scale`` (guide pixels per main pixel) should come from the
    two optical trains' known plate scale — see
    ``astrotool_core.optics.smarttscope_config`` — as a starting point;
    the search covers ``approx_scale * (1 +/- scale_search_fraction)`` to
    absorb the config being slightly off, not to discover scale from
    scratch. Pass ``scale_steps=1`` to trust ``approx_scale`` exactly and
    skip the scale search entirely (cheaper).

    Performance: measured on a real rig (ATR585M 3840x2160 main,
    GPCMOS02000KPA 1920x1080 guide) at ~0.6s per (scale, angle)
    correlation at full resolution — the defaults above (72 angles x 3
    scales = 216 candidates) take roughly two real minutes end to end,
    reported by real-world use as "very slow" for an interactive tool.
    ``search_downsample`` (see below) is the main lever for that; the
    rotation range itself defaults to the full circle since a guide
    scope's mounting angle relative to the main OTA isn't assumed to be
    small — narrow ``angle_range_deg`` if you already have a rough idea
    (e.g. after an initial calibration, to refine it faster), or widen
    ``angle_step_deg``/reduce ``scale_steps`` for an even coarser pass.
    Always call this off the UI thread — see `FovCalibrator`.

    ``search_downsample`` (an integer factor, e.g. 4) shrinks both
    images before searching — FFT cost drops roughly with the square of
    it, so 4x fewer pixels per axis is roughly 16x less work per
    candidate — then scales the returned rectangle's position/size back
    up to ``guide_mono``'s original resolution. Rotation accuracy is
    unaffected (it doesn't depend on resolution); position accuracy
    degrades by up to ``search_downsample`` guide-pixels, which a visual
    overlay guide doesn't need to be exact to. Defaults to 1 (no
    downsampling, exact) since this is also used directly in tests that
    check exact pixel positions — `FovCalibrator`'s production caller
    passes a larger value.

    Featureless regions: if the main camera happens to be framed on an
    area with little real structure (e.g. a flat twilight sky, or a
    landscape crushed to near-black by exposure tuned for a much
    brighter sky elsewhere in the same guide frame — confirmed by real
    use), naive NCC can still report a spuriously "confident" match:
    dividing two near-zero numbers (a near-flat template's tiny residual
    noise against an equally near-flat window) is numerically unstable
    and can land anywhere. Before scoring a given scale's resized
    template at all, this requires its standard deviation to be at least
    ``min_relative_contrast`` times ``guide_mono``'s own overall standard
    deviation — a self-calibrating floor (no absolute units/exposure
    assumptions needed) that treats "not enough real signal to match
    against" the same as "no confident match" rather than reporting a
    location with nothing actually recognizable there.

    Out-of-focus/low-detail images: a real incident with a heavily
    out-of-focus main camera (sky and a blurred landscape/utility pole,
    no resolved stars) still scored a "confident" ~0.65 — the matched
    window had plenty of standard deviation (passing the contrast floor
    above with room to spare), but almost all of it came from a smooth
    brightness gradient (sky glow, or landscape-to-sky transition) that
    happened to weakly resemble the template's own blur, not from
    genuine matchable structure. Overall contrast can't tell a smooth
    gradient apart from real detail; this instead requires both images'
    ``_sharpness_ratio`` (high-frequency gradient energy relative to
    variance — see that function) to reach ``min_sharpness_ratio``,
    checked once up front (before the search starts, so a hopeless pair
    fails fast rather than after a full search).

    Returns ``None`` if no candidate reaches ``min_score`` (a real match
    typically scores well above it; unrelated frames score near 0) — the
    caller should keep whatever rectangle it already had rather than
    trust a bad match. Also returns ``None`` if every candidate scale
    would make the resized template larger than the guide frame on
    either axis (an ``approx_scale`` far too large for this pair of
    frames), if every candidate scale fails the contrast floor above, or
    if either image fails the sharpness check above.

    ``progress_callback``, if given, is called as ``callback(completed,
    total)`` after each (scale, angle) candidate is scored — the search
    takes long enough (see "Performance" above) that a caller running
    this off the UI thread should report progress rather than leave the
    user watching a static "working" message with no sign anything is
    happening (see `FovCalibrator`).

    Not for per-frame use — see the module docstring's "Deliberately a
    one-shot, explicitly-triggered calibration".
    """
    if main_mono.ndim != 2 or guide_mono.ndim != 2:
        raise ValueError("main_mono and guide_mono must be 2D mono arrays")
    if approx_scale <= 0.0:
        raise ValueError("approx_scale must be positive")

    if (
        _sharpness_ratio(main_mono) < min_sharpness_ratio
        or _sharpness_ratio(guide_mono) < min_sharpness_ratio
    ):
        # Fail fast, before the (slow) search — see "Out-of-focus/
        # low-detail images" above.
        return None

    downsample = max(1, int(search_downsample))
    if downsample > 1:
        main_search = _resize_bilinear(
            main_mono,
            max(1, main_mono.shape[0] // downsample),
            max(1, main_mono.shape[1] // downsample),
        )
        guide_search = _resize_bilinear(
            guide_mono,
            max(1, guide_mono.shape[0] // downsample),
            max(1, guide_mono.shape[1] // downsample),
        )
    else:
        main_search = main_mono
        guide_search = guide_mono

    main_h, main_w = main_search.shape
    guide_h, guide_w = guide_search.shape
    guide_std = float(guide_search.std())
    contrast_floor = min_relative_contrast * guide_std if guide_std > 0.0 else 0.0

    if scale_steps <= 1:
        scale_candidates = [approx_scale]
    else:
        low = approx_scale * (1.0 - scale_search_fraction)
        high = approx_scale * (1.0 + scale_search_fraction)
        scale_candidates = list(np.linspace(low, high, scale_steps))

    angles = np.arange(angle_range_deg[0], angle_range_deg[1], angle_step_deg)

    # Filter out-of-range and too-flat-to-match scales up front so the
    # total candidate count — and therefore progress — is known before
    # the (slow) search starts, rather than discovered candidate by
    # candidate.
    valid_scales: list[tuple[float, int, int]] = []
    for scale in scale_candidates:
        scaled_h = max(1, round(main_h * scale))
        scaled_w = max(1, round(main_w * scale))
        if scaled_h > guide_h or scaled_w > guide_w:
            continue
        if float(_resize_bilinear(main_search, scaled_h, scaled_w).std()) < contrast_floor:
            continue
        valid_scales.append((scale, scaled_h, scaled_w))
    total_candidates = len(valid_scales) * len(angles)

    best: FovRegistrationResult | None = None
    completed = 0
    for scale, scaled_h, scaled_w in valid_scales:
        resized = _resize_bilinear(main_search, scaled_h, scaled_w)
        fill_value = float(resized.mean())
        for angle in angles:
            rotated = _rotate_bilinear(resized, float(angle), fill_value)
            surface = _normalized_cross_correlation_surface(guide_search, rotated)
            row, col = np.unravel_index(int(np.argmax(surface)), surface.shape)
            score = float(surface[row, col])
            if best is None or score > best.score:
                best = FovRegistrationResult(
                    center_x_px=(col + scaled_w / 2.0) * downsample,
                    center_y_px=(row + scaled_h / 2.0) * downsample,
                    width_px=float(scaled_w) * downsample,
                    height_px=float(scaled_h) * downsample,
                    rotation_deg=float(angle),
                    scale=float(scale),
                    score=score,
                )
            completed += 1
            if progress_callback is not None:
                progress_callback(completed, total_candidates)

    if best is None or best.score < min_score:
        return None
    return best


def registration_corners(result: FovRegistrationResult) -> list[tuple[float, float]]:
    """The matched rectangle's four corners (top-left, top-right,
    bottom-right, bottom-left of the *unrotated* main sensor) in
    guide-frame pixel coordinates, honoring ``rotation_deg`` — for
    drawing an actual rotated rectangle rather than an axis-aligned
    bounding box.

    Uses the same forward-rotation convention as ``_rotate_bilinear``:
    ``corner = center + R(rotation_deg) @ corner_relative_to_center``.
    """
    half_w = result.width_px / 2.0
    half_h = result.height_px / 2.0
    theta = np.deg2rad(result.rotation_deg)
    cos_t, sin_t = np.cos(theta), np.sin(theta)
    corners_relative = [(-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h)]
    corners = []
    for lx, ly in corners_relative:
        rx = cos_t * lx - sin_t * ly
        ry = sin_t * lx + cos_t * ly
        corners.append((result.center_x_px + rx, result.center_y_px + ry))
    return corners
