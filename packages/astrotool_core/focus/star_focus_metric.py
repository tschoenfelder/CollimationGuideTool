"""Star-mode autofocus analyzer — issue #33's own "Mode A" requirement: a
star-specific metric/detector, separate from the terrestrial implementation,
that rejects unsuitable stars and aggregates robustly across multiple
usable ones.

Built on the existing, already-tested `astrotool_core.target.detect_sources`/
`PointSource` primitives, plus the two pieces confirmed genuinely missing
elsewhere in this codebase: real saturated/donut *rejection* (not
`roi_selector.select_target`'s mere de-prioritization of a single best
target) and edge-clip rejection (nothing else in this project checks a
source's own distance from the frame boundary). `core` never imports the
`apps` layer (see CONTRIBUTING.md's import-linter contract), so this
averages `fwhm_x`/`fwhm_y` directly rather than reusing
`collimation_tool.domain.focus_metric.mean_fwhm_px` -- the same trivial
one-line average, kept independent on purpose.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

import numpy as np

from astrotool_core.target.detector import detect_sources
from astrotool_core.target.point_source import PointSource

#: Usable-star count at which aggregate confidence reaches 1.0 -- issue
#: #33's own "continue to work when only one suitable star is available,
#: with appropriately reduced confidence" (a single star reports 1/3).
_FULL_CONFIDENCE_STAR_COUNT = 3


@dataclass(frozen=True)
class StarFocusMeasurement:
    """`fwhm_px` is the robust (median) aggregate across every usable
    star in this one frame -- `None` if no star cleared every rejection
    guard. `confidence` scales with `usable_star_count` up to
    `_FULL_CONFIDENCE_STAR_COUNT`, never `0.0` for at least one usable
    star (issue #33: a single suitable star must still produce a usable,
    if reduced-confidence, measurement)."""

    fwhm_px: float | None
    usable_star_count: int
    confidence: float
    rejected_saturated: int
    rejected_edge: int
    rejected_donut: int


def measure_star_focus(frame: np.ndarray, *, edge_margin_px: int = 16) -> StarFocusMeasurement:
    """Detect sources in `frame` (a 2D mono analysis plane) and aggregate
    a robust FWHM figure across every usable one -- see
    `StarFocusMeasurement`'s own docstring for exactly what "usable"
    excludes."""
    detection = detect_sources(frame)
    height, width = frame.shape

    usable_fwhm: list[float] = []
    rejected_saturated = 0
    rejected_edge = 0
    rejected_donut = 0

    for source in detection.sources:
        if source.donut_like:
            rejected_donut += 1
            continue
        if source.saturated:
            rejected_saturated += 1
            continue
        if (
            source.x < edge_margin_px
            or source.x > width - edge_margin_px
            or source.y < edge_margin_px
            or source.y > height - edge_margin_px
        ):
            rejected_edge += 1
            continue
        if source.fwhm_x is None or source.fwhm_y is None:
            continue  # no usable width measurement for this source
        usable_fwhm.append((source.fwhm_x + source.fwhm_y) / 2.0)

    if not usable_fwhm:
        return StarFocusMeasurement(
            fwhm_px=None,
            usable_star_count=0,
            confidence=0.0,
            rejected_saturated=rejected_saturated,
            rejected_edge=rejected_edge,
            rejected_donut=rejected_donut,
        )

    fwhm_px = float(statistics.median(usable_fwhm))
    confidence = min(1.0, len(usable_fwhm) / _FULL_CONFIDENCE_STAR_COUNT)
    return StarFocusMeasurement(
        fwhm_px=fwhm_px,
        usable_star_count=len(usable_fwhm),
        confidence=confidence,
        rejected_saturated=rejected_saturated,
        rejected_edge=rejected_edge,
        rejected_donut=rejected_donut,
    )


@dataclass(frozen=True)
class TrackedStarMeasurement:
    """One sample of the tracked target. `fwhm_px` is `None` exactly when
    `reason` is set -- `reason` is one of `no_star`, `multiple_candidates`,
    `saturated`, `donut_like`, `edge_clipped`, `not_found_at_tracked_position`,
    `no_width`."""

    fwhm_px: float | None
    reason: str | None
    centroid: tuple[float, float] | None = None


class StarTargetTracker:
    """Issue #33 (single artificial star): keeps ONE target's identity
    through a focus sweep. `acquire()` requires exactly one candidate
    (never silently picking between several); `measure()` only ever looks at
    the source nearest the last tracked position within `max_shift_px` --
    a different bright point elsewhere in the frame is ignored, never
    substituted. A sample that cannot be measured says why (see
    `TrackedStarMeasurement`) instead of being replaced by something else.
    """

    def __init__(self, *, edge_margin_px: int = 16, max_shift_px: float = 40.0) -> None:
        self._edge_margin_px = edge_margin_px
        self._max_shift_px = max_shift_px
        self._target: tuple[float, float] | None = None

    @property
    def target(self) -> tuple[float, float] | None:
        return self._target

    def _near_edge(self, x: float, y: float, shape: tuple[int, int]) -> bool:
        height, width = shape
        margin = self._edge_margin_px
        return x < margin or x > width - margin or y < margin or y > height - margin

    def _classify(self, source: PointSource, shape: tuple[int, int]) -> TrackedStarMeasurement:
        centroid = (source.x, source.y)
        if self._near_edge(source.x, source.y, shape):
            return TrackedStarMeasurement(None, "edge_clipped", centroid)
        if source.saturated:
            return TrackedStarMeasurement(None, "saturated", centroid)
        if source.donut_like:
            return TrackedStarMeasurement(None, "donut_like", centroid)
        if source.fwhm_x is None or source.fwhm_y is None:
            return TrackedStarMeasurement(None, "no_width", centroid)
        return TrackedStarMeasurement((source.fwhm_x + source.fwhm_y) / 2.0, None, centroid)

    def acquire(self, frame: np.ndarray) -> TrackedStarMeasurement:
        sources = detect_sources(frame).sources
        if not sources:
            return TrackedStarMeasurement(None, "no_star")
        if len(sources) > 1:
            return TrackedStarMeasurement(None, "multiple_candidates")
        result = self._classify(sources[0], frame.shape)
        if result.reason in (None, "saturated", "donut_like"):
            # Identity is fixed even if this first sample isn't usable
            # (e.g. saturated): later samples must follow the SAME source.
            self._target = result.centroid
        return result

    def measure(self, frame: np.ndarray) -> TrackedStarMeasurement:
        if self._target is None:
            return TrackedStarMeasurement(None, "no_star")
        tx, ty = self._target
        nearby = [
            s
            for s in detect_sources(frame).sources
            if math.hypot(s.x - tx, s.y - ty) <= self._max_shift_px
        ]
        if not nearby:
            return TrackedStarMeasurement(None, "not_found_at_tracked_position")
        source = min(nearby, key=lambda s: math.hypot(s.x - tx, s.y - ty))
        result = self._classify(source, frame.shape)
        if result.centroid is not None and result.reason != "edge_clipped":
            self._target = result.centroid
        return result
