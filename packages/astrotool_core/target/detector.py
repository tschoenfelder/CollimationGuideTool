"""Single-frame star detection, built on smarttscope_live_analysis.

Wraps ``smarttscope_live_analysis.analysis.analyze_frame`` and adapts its
``DetectedSource`` records into this project's ``PointSource`` type.
Multi-frame temporal tracking and lock-state management live in
``roi_tracker.py`` (Stage 3), not here — this module is single-frame only.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from smarttscope_live_analysis.analysis import DetectedSource, analyze_frame

from astrotool_core.target.point_source import PointSource


@dataclass(frozen=True)
class DetectionResult:
    """Result of detecting point sources in one frame."""

    sources: tuple[PointSource, ...]
    image_quality: str
    focus_warning: str | None
    notes: tuple[str, ...]


def detect_sources(
    image: np.ndarray,
    *,
    exposure_s: float | None = None,
    gain: int | None = None,
    offset: int | None = None,
) -> DetectionResult:
    """Detect point sources in a single 2D image plane.

    Args:
        image: 2D array (mono analysis plane).
        exposure_s, gain, offset: current camera settings, used only to
            shape suggested-adjustment notes; pass None if unknown.
    """
    result = analyze_frame(image, exposure_s=exposure_s, gain=gain, offset=offset)
    sources = tuple(_to_point_source(source) for source in result.sources)
    return DetectionResult(
        sources=sources,
        image_quality=result.image_quality,
        focus_warning=result.focus_warning,
        notes=result.notes,
    )


def _to_point_source(source: DetectedSource) -> PointSource:
    return PointSource(
        x=source.x,
        y=source.y,
        peak=source.peak,
        area=source.area,
        kind=source.kind,
        fwhm_x=source.fwhm_x,
        fwhm_y=source.fwhm_y,
        saturated=source.saturated,
        donut_like=source.donut_like,
    )
