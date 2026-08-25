"""Synthetic frame builders for tests: Gaussian stars, hot pixels, Bayer mosaics.

No hardware, no FITS I/O — used by tests/core and, later, by collimation/
guide characterization tests that need a controllable star field.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from astropy.io import fits

from astrotool_core.frames.frame import Frame
from astrotool_core.frames.pixel_format import BayerPattern, mosaic_from_rgb


@dataclass(frozen=True)
class StarSpec:
    """One synthetic Gaussian star to render."""

    x: float
    y: float
    peak: float
    sigma: float = 2.5


def star_field_image(
    shape: tuple[int, int],
    stars: list[StarSpec],
    *,
    background: float = 100.0,
) -> np.ndarray:
    """Render a float32 mono image containing one or more Gaussian stars."""
    height, width = shape
    image = np.full((height, width), background, dtype=np.float32)
    yy, xx = np.indices((height, width), dtype=np.float32)
    for star in stars:
        image += star.peak * np.exp(
            -(((xx - star.x) ** 2 + (yy - star.y) ** 2) / (2.0 * star.sigma**2))
        )
    return image


def single_star_image(
    shape: tuple[int, int],
    *,
    x: float,
    y: float,
    peak: float,
    sigma: float = 2.5,
    background: float = 100.0,
) -> np.ndarray:
    """Render a float32 mono image containing exactly one Gaussian star."""
    return star_field_image(
        shape,
        [StarSpec(x=x, y=y, peak=peak, sigma=sigma)],
        background=background,
    )


def with_hot_pixels(
    image: np.ndarray,
    positions: list[tuple[int, int]],
    *,
    value: float = 65000.0,
) -> np.ndarray:
    """Return a copy of ``image`` with single-pixel hot pixels injected at (y, x)."""
    out = image.copy()
    for y, x in positions:
        out[y, x] = value
    return out


def bayer_star_field_image(
    shape: tuple[int, int],
    stars: list[StarSpec],
    pattern: BayerPattern,
    *,
    background: float = 100.0,
    star_color: tuple[float, float, float] = (1.0, 1.0, 1.0),
) -> np.ndarray:
    """Render a mosaiced Bayer plane containing Gaussian stars of a given color."""
    height, width = shape
    rgb = np.full((height, width, 3), background, dtype=np.float32)
    yy, xx = np.indices((height, width), dtype=np.float32)
    for star in stars:
        profile = star.peak * np.exp(
            -(((xx - star.x) ** 2 + (yy - star.y) ** 2) / (2.0 * star.sigma**2))
        )
        for channel, weight in enumerate(star_color):
            rgb[..., channel] += profile * weight
    return mosaic_from_rgb(rgb, pattern)


def make_frame(
    pixels: np.ndarray,
    *,
    exposure_seconds: float = 1.0,
    bit_depth: int = 16,
) -> Frame:
    """Wrap a synthetic pixel array into a Frame, for tests that need one."""
    return Frame(
        pixels=pixels.astype(np.float32, copy=False),
        header=fits.Header(),
        exposure_seconds=exposure_seconds,
        bit_depth=bit_depth,
    )
