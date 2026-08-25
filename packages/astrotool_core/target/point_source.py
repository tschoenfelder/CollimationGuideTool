"""PointSource — thin, hardware-agnostic wrapper around a detected star.

Adapts ``smarttscope_live_analysis.analysis.DetectedSource`` into this
project's own type, so callers never depend on the library's dataclass
shape directly (per CONTRIBUTING.md's public-interface rule).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PointSource:
    """One compact detected source in image-space pixel coordinates."""

    x: float
    y: float
    peak: float
    area: int
    kind: str
    fwhm_x: float | None = None
    fwhm_y: float | None = None
    saturated: bool = False
    donut_like: bool = False
