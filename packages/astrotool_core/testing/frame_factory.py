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


def airy_pattern_image(
    shape: tuple[int, int],
    *,
    x: float,
    y: float,
    peak: float,
    core_sigma: float,
    background: float = 100.0,
    ring_radius_px: float | None = None,
    ring_peak: float = 0.0,
    ring_sigma: float = 1.5,
) -> np.ndarray:
    """Render a float32 mono image: a central Gaussian core plus an
    optional Gaussian "ring" (a thin annulus of added intensity at
    `ring_radius_px`) -- a synthetic APPROXIMATION of an Airy
    diffraction pattern for deterministic testing (issue #18), not a
    physically exact Airy/Bessel-function render (this project has no
    scipy dependency). Built independently of Stage 4's own radial-
    binning code (`astrotool_core.diffraction.radial_profile`) -- a
    test-fixture concern, not the module under test. `ring_peak=0.0`
    (default) reproduces a plain Gaussian PSF with no ring at all."""
    height, width = shape
    image = np.full((height, width), background, dtype=np.float32)
    yy, xx = np.indices((height, width), dtype=np.float32)
    r2 = (xx - x) ** 2 + (yy - y) ** 2
    image += peak * np.exp(-(r2 / (2.0 * core_sigma**2)))
    if ring_peak > 0.0 and ring_radius_px is not None:
        r = np.sqrt(r2)
        image += ring_peak * np.exp(-(((r - ring_radius_px) ** 2) / (2.0 * ring_sigma**2)))
    return image


def coma_pattern_image(
    shape: tuple[int, int],
    *,
    x: float,
    y: float,
    peak: float,
    core_sigma: float,
    ring_radius_px: float,
    ring_peak: float,
    background: float = 100.0,
    ring_sigma: float = 1.5,
    asymmetry_direction_deg: float = 0.0,
    asymmetry_strength: float = 0.0,
) -> np.ndarray:
    """Like `airy_pattern_image`, but the ring's own intensity is
    azimuthally modulated by `1 + asymmetry_strength * cos(theta -
    direction)` -- brighter toward `asymmetry_direction_deg`, dimmer on
    the opposite side. A synthetic APPROXIMATION of coma/collimation-
    error asymmetry for deterministic testing (issue #20), not
    physically exact coma optics. `theta` uses the same `atan2(dy, dx)`
    convention `astrotool_core.diffraction.symmetry_measurement` itself
    uses, so an imposed direction here is directly comparable to a
    measured one there. `asymmetry_strength=0.0` (default) reproduces a
    plain symmetric ring, same as `airy_pattern_image`'s own."""
    height, width = shape
    image = np.full((height, width), background, dtype=np.float32)
    yy, xx = np.indices((height, width), dtype=np.float32)
    dx, dy = xx - x, yy - y
    r2 = dx**2 + dy**2
    image += peak * np.exp(-(r2 / (2.0 * core_sigma**2)))
    if ring_peak > 0.0:
        r = np.sqrt(r2)
        theta = np.arctan2(dy, dx)
        direction_rad = np.radians(asymmetry_direction_deg)
        modulation = 1.0 + asymmetry_strength * np.cos(theta - direction_rad)
        image += (
            ring_peak * modulation * np.exp(-(((r - ring_radius_px) ** 2) / (2.0 * ring_sigma**2)))
        )
    return image


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


def donut_image(
    shape: tuple[int, int],
    *,
    outer_center: tuple[float, float],
    outer_radius: float,
    inner_center: tuple[float, float],
    inner_radius: float,
    peak: float,
    background: float = 100.0,
) -> np.ndarray:
    """Render a float32 mono image of a defocused-star donut (ring with a dark hole).

    ``outer_center``/``outer_radius`` describe the bright outer ring;
    ``inner_center``/``inner_radius`` describe the dark inner hole
    (secondary-mirror shadow) — offsetting ``inner_center`` from
    ``outer_center`` simulates a collimation error.
    """
    height, width = shape
    yy, xx = np.indices((height, width), dtype=np.float32)
    outer_dist = np.hypot(xx - outer_center[0], yy - outer_center[1])
    inner_dist = np.hypot(xx - inner_center[0], yy - inner_center[1])
    ring = (outer_dist <= outer_radius) & (inner_dist >= inner_radius)
    image = np.full((height, width), background, dtype=np.float32)
    image[ring] += peak
    return image


def with_shadow(
    image: np.ndarray,
    *,
    center: tuple[float, float],
    radius: float,
    depth: float,
) -> np.ndarray:
    """Return a copy of ``image`` with a circular shadow (darkened disk) added."""
    height, width = image.shape
    yy, xx = np.indices((height, width), dtype=np.float32)
    dist = np.hypot(xx - center[0], yy - center[1])
    mask = dist <= radius
    out = image.copy()
    out[mask] -= depth
    return out


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
