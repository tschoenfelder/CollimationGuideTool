"""Pixel format handling: Bayer pattern identification and demosaicing.

New in this repo (no analog in smart_telescope or smarttscope-live-analysis,
both of which only ever handle already-mono planes). Bilinear demosaic:
each output channel is reconstructed by convolving its sparse, masked
samples with a small interpolation kernel and normalizing by the same
kernel applied to the sampling mask.
"""

from __future__ import annotations

import enum

import numpy as np


class BayerPattern(enum.Enum):
    """Sensor pixel layout. MONO means no color filter array is present."""

    MONO = "mono"
    RGGB = "rggb"
    BGGR = "bggr"
    GRBG = "grbg"
    GBRG = "gbrg"


def is_bayer(pattern: BayerPattern) -> bool:
    """True if ``pattern`` describes a color filter array (needs demosaicing)."""
    return pattern is not BayerPattern.MONO


# (row_parity, col_parity) -> channel, for each 2x2 mosaic tile.
_PATTERN_LAYOUT: dict[BayerPattern, dict[tuple[int, int], str]] = {
    BayerPattern.RGGB: {(0, 0): "r", (0, 1): "g", (1, 0): "g", (1, 1): "b"},
    BayerPattern.BGGR: {(0, 0): "b", (0, 1): "g", (1, 0): "g", (1, 1): "r"},
    BayerPattern.GRBG: {(0, 0): "g", (0, 1): "r", (1, 0): "b", (1, 1): "g"},
    BayerPattern.GBRG: {(0, 0): "g", (0, 1): "b", (1, 0): "r", (1, 1): "g"},
}

_RB_KERNEL = np.array([[1.0, 2.0, 1.0], [2.0, 4.0, 2.0], [1.0, 2.0, 1.0]], dtype=np.float32)
_G_KERNEL = np.array([[0.0, 1.0, 0.0], [1.0, 4.0, 1.0], [0.0, 1.0, 0.0]], dtype=np.float32)

# ITU-R BT.601 luma weights — shared by every caller that needs a single
# mono view of a demosaiced RGB frame (donut/star geometry analysis cares
# about spatial intensity, not color science, and this is also the plane
# CollimationGuideTool's live-view display uses to pick its stretch
# range — see FrameAnalyzer and live_view.stretch_rgb_to_uint8).
_LUMA_R, _LUMA_G, _LUMA_B = 0.299, 0.587, 0.114


def rgb_to_luma(rgb: np.ndarray) -> np.ndarray:
    """Collapse an (H, W, 3) RGB image to a single-channel luma plane."""
    luma = _LUMA_R * rgb[..., 0] + _LUMA_G * rgb[..., 1] + _LUMA_B * rgb[..., 2]
    result: np.ndarray = luma.astype(np.float32)
    return result


def gray_world_white_balance(rgb: np.ndarray) -> np.ndarray:
    """Scale each of an (H, W, 3) RGB image's channels so their means match
    -- the "gray world" assumption (a real scene's average color, over
    enough pixels, is roughly neutral gray) -- for **display only**.

    Real diagnostics 3bc76175/99926503: Guide (`GPCMOS02000KPA`) confirmed
    a genuine Bayer color sensor whose green photosites read
    substantially higher than red/blue even in the raw, pre-demosaic
    data (~1.4-3x in real captures) -- `demosaic()` itself reconstructs
    each channel correctly (see that function's own docstring), but
    nothing anywhere corrects for the sensor's own per-channel
    sensitivity difference, so a real, uncorrected green cast passes
    straight through to the live view (confirmed visually against the
    real saved display image: strongly green-dominant, not a neutral
    starfield). This is deliberately *not* applied before `rgb_to_luma()`
    for detection/measurement -- a separate real evidence check (same
    diagnostics) found `detect_sources()` actually performs *better* on
    the current, unbalanced luma than on other candidate corrections
    tried for that purpose; this function exists solely to make the
    operator's own live view look right, scoped to
    `stretch_rgb_to_uint8()`'s own caller.

    Each channel's own mean is computed from the whole frame (cheap
    relative to the demosaic/stretch work already done on it) and scaled
    by `overall_mean / channel_mean`, clamped so a channel with an
    already-near-zero mean (a genuinely black frame) can't blow up into
    a huge gain -- same defensive shape as `live_view.py`'s own
    `hi <= lo` flat-frame handling."""
    rgb = np.asarray(rgb, dtype=np.float32)
    channel_means = rgb.reshape(-1, 3).mean(axis=0)
    overall_mean = float(channel_means.mean())
    if overall_mean <= 0.0:
        return rgb
    # A channel whose own mean is a tiny fraction of the overall mean
    # would otherwise solve for an enormous gain (amplifying that
    # channel's own noise floor into visible garbage) -- capped at 4x,
    # comfortably above the ~3x green/blue ratio real captures showed,
    # but not so high a near-black channel gets blown out.
    gains = np.clip(overall_mean / np.maximum(channel_means, overall_mean * 0.05), 0.25, 4.0)
    balanced: np.ndarray = rgb * gains
    return balanced


def demosaic(plane: np.ndarray, pattern: BayerPattern) -> np.ndarray:
    """Convert a single mosaiced (or mono) plane into an (H, W, 3) float32 RGB image.

    For ``BayerPattern.MONO`` this is a passthrough that duplicates the
    plane into all three channels. For a Bayer pattern, each channel is
    reconstructed by bilinear interpolation of its sampled pixels.
    """
    plane = np.asarray(plane, dtype=np.float32)

    if pattern is BayerPattern.MONO:
        return np.stack([plane, plane, plane], axis=-1).astype(np.float32)

    if plane.shape[0] % 2 or plane.shape[1] % 2:
        raise ValueError("Bayer mosaic dimensions must be even, got shape " f"{plane.shape}")

    mask_r, mask_g, mask_b = _channel_masks(pattern, plane.shape)
    r = _interpolate(plane * mask_r, mask_r, _RB_KERNEL)
    g = _interpolate(plane * mask_g, mask_g, _G_KERNEL)
    b = _interpolate(plane * mask_b, mask_b, _RB_KERNEL)
    return np.stack([r, g, b], axis=-1).astype(np.float32)


def _channel_masks(
    pattern: BayerPattern,
    shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mask_r = np.zeros(shape, dtype=np.float32)
    mask_g = np.zeros(shape, dtype=np.float32)
    mask_b = np.zeros(shape, dtype=np.float32)
    targets = {"r": mask_r, "g": mask_g, "b": mask_b}
    for (dy, dx), channel in _PATTERN_LAYOUT[pattern].items():
        targets[channel][dy::2, dx::2] = 1.0
    return mask_r, mask_g, mask_b


def _interpolate(masked: np.ndarray, mask: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    numerator = _convolve3x3(masked, kernel)
    denominator = _convolve3x3(mask, kernel)
    result: np.ndarray = np.divide(
        numerator,
        denominator,
        out=np.zeros_like(numerator),
        where=denominator > 0,
    )
    return result


def mosaic_from_rgb(rgb: np.ndarray, pattern: BayerPattern) -> np.ndarray:
    """Sample an (H, W, 3) RGB image down into a single mosaiced plane.

    Lossy inverse of the sampling ``demosaic`` reconstructs from: each
    output pixel keeps only the channel it would see under ``pattern``.
    Used by synthetic test fixtures to build realistic Bayer input.
    """
    rgb = np.asarray(rgb, dtype=np.float32)
    if pattern is BayerPattern.MONO:
        return rgb.mean(axis=-1).astype(np.float32)

    height, width = rgb.shape[:2]
    if height % 2 or width % 2:
        raise ValueError(
            "Bayer mosaic dimensions must be even, got shape " f"{rgb.shape[:2]}"
        )

    channel_index = {"r": 0, "g": 1, "b": 2}
    mosaic = np.zeros((height, width), dtype=np.float32)
    for (dy, dx), channel in _PATTERN_LAYOUT[pattern].items():
        mosaic[dy::2, dx::2] = rgb[dy::2, dx::2, channel_index[channel]]
    return mosaic


def _convolve3x3(image: np.ndarray, kernel: np.ndarray) -> np.ndarray:
    padded = np.pad(image, 1, mode="edge")
    height, width = image.shape
    out = np.zeros_like(image)
    for dy in range(3):
        for dx in range(3):
            weight = kernel[dy, dx]
            if weight == 0:
                continue
            out += weight * padded[dy : dy + height, dx : dx + width]
    return out
