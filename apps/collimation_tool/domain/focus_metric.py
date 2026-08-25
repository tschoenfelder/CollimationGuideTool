"""Focus quality metric — classifies a measured FWHM into a quality tier.

New: no direct 1:1 port. Reuses the quality-tier thresholds from
smart_telescope's `FWHMFocusController._quality()` (excellent/good/poor),
but built on `astrotool_core.target.PointSource.fwhm_x`/`fwhm_y` (from
`smarttscope-live-analysis`'s own moment-based FWHM estimator) instead of
porting `star_detection.py`'s separate radial-profile FWHM estimator —
see docs/porting-notes.md for why.
"""

from __future__ import annotations

from dataclasses import dataclass

_EXCELLENT_FWHM_PX = 2.0
_GOOD_FWHM_PX = 4.0


@dataclass(frozen=True)
class FocusQuality:
    fwhm_px: float
    tier: str  # "excellent" | "good" | "poor"

    @property
    def is_in_focus(self) -> bool:
        return self.tier != "poor"


def classify_focus_quality(
    fwhm_px: float,
    *,
    excellent_fwhm_px: float = _EXCELLENT_FWHM_PX,
    good_fwhm_px: float = _GOOD_FWHM_PX,
) -> FocusQuality:
    """Classify a measured FWHM (pixels) into excellent/good/poor."""
    if fwhm_px <= excellent_fwhm_px:
        tier = "excellent"
    elif fwhm_px <= good_fwhm_px:
        tier = "good"
    else:
        tier = "poor"
    return FocusQuality(fwhm_px=fwhm_px, tier=tier)


def mean_fwhm_px(fwhm_x: float, fwhm_y: float) -> float:
    """Average PointSource.fwhm_x/fwhm_y into one scalar focus figure."""
    return (fwhm_x + fwhm_y) / 2.0
