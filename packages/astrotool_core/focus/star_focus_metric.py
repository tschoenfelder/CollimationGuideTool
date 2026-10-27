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
from dataclasses import dataclass, replace

import numpy as np

from astrotool_core.focus.star_candidates import (
    FrameStatistics,
    StarCandidate,
    binning_for,
    find_star_candidates,
    frame_statistics,
    is_clipped,
)
from astrotool_core.target.detector import detect_sources

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
    """One sample of the tracked target. `size_px` is the target's half-flux diameter
    (issue #58: one blob-level size for the whole sweep, see `star_candidates`); it is `None`
    exactly when `reason` is set -- `reason` is one of `no_star`, `multiple_candidates`,
    `saturated`, `edge_clipped`, `not_found_at_tracked_position`, `no_width`."""

    size_px: float | None
    reason: str | None
    centroid: tuple[float, float] | None = None


#: Issue #58: two candidates whose distances to the image centre (or to the tracked position)
#: differ by less than this are equally near -- picking one would be a guess, so the tracker
#: reports `multiple_candidates` instead.
CENTRE_TIE_PX = 2.0
#: Issue #58 (F4): `measure()` analyses a crop of +- (max shift + this many blob radii + 32 px)
#: around the target instead of the whole frame.
_CROP_RADII = 3.0
_CROP_MARGIN_PX = 32


class StarTargetTracker:
    """Issue #33 (artificial star): keeps ONE target's identity through a focus sweep.

    Issue #58: candidates are star blobs (`find_star_candidates`: a clipped plateau with its
    halo is one candidate, a big defocused star or donut is found too) and the sample value is
    the blob's half-flux diameter, which keeps shrinking towards focus for stars of any size.
    With several candidates `acquire()` follows the one nearest the image centre (the green
    centre cross) and says so via `candidate_count` -- two equally near candidates are refused
    as `multiple_candidates`, never guessed. `measure()` only ever looks at the candidate
    nearest the last tracked position within `max_shift_px` (or half the blob's radius, if
    larger) and analyses only a crop around it; a different bright point elsewhere in the frame
    is ignored, never substituted, and two equally near candidates there are
    `multiple_candidates`. A sample that cannot be measured says why (see
    `TrackedStarMeasurement`) instead of being replaced by something else.
    """

    def __init__(
        self,
        *,
        edge_margin_px: int = 16,
        max_shift_px: float = 40.0,
        full_scale: float | None = None,
        full_scale_floor: float = 0.0,
    ) -> None:
        """`full_scale` (review N3): the camera's full scale (`2**bit_depth - 1`); when None,
        it is inferred from the frames (`detect_sources`' rule) and the largest value seen --
        at least `full_scale_floor`, the one an earlier tracker of the run inferred -- is kept;
        read it back via `full_scale`."""
        self._known_full_scale = full_scale
        self._edge_margin_px = edge_margin_px
        self._max_shift_px = max_shift_px
        self._target: tuple[float, float] | None = None
        self._radius_px = 0.0
        self._candidate_count = 0
        self._binning: int | None = None
        self._full_scale = full_scale or full_scale_floor
        self._last_clipped = False

    @property
    def full_scale(self) -> float:
        """The full scale in use (0 before the first frame when it is inferred)."""
        return self._full_scale

    @property
    def target(self) -> tuple[float, float] | None:
        return self._target

    @property
    def candidate_count(self) -> int:
        """How many star candidates the last `acquire()` saw (0 before it)."""
        return self._candidate_count

    @property
    def last_frame_clipped(self) -> bool:
        """The last analysed frame had clipped pixels (>= 98 % of full scale)."""
        return self._last_clipped

    def reset(self) -> None:
        """Forget the target (the next call must be `acquire()`)."""
        self._target = None
        self._radius_px = 0.0
        self._candidate_count = 0

    def _statistics(self, frame: np.ndarray) -> FrameStatistics:
        if self._binning is None:
            self._binning = binning_for(frame.shape)
        stats = frame_statistics(frame, self._binning, full_scale=self._known_full_scale)
        if self._known_full_scale is None:
            # Inferred: a frame lowered far enough can lose every value above 4096; the full
            # scale of the camera does not change within a sweep, so the largest one is kept.
            self._full_scale = max(self._full_scale, stats.full_scale)
            stats = replace(stats, full_scale=self._full_scale)
        self._last_clipped = is_clipped(frame, self._full_scale)
        return stats

    def _near_edge(self, x: float, y: float, shape: tuple[int, int]) -> bool:
        height, width = shape
        margin = self._edge_margin_px
        return x < margin or x > width - margin or y < margin or y > height - margin

    def _classify(self, candidate: StarCandidate, shape: tuple[int, int]) -> TrackedStarMeasurement:
        centroid = (candidate.x, candidate.y)
        if candidate.touches_edge or self._near_edge(candidate.x, candidate.y, shape):
            return TrackedStarMeasurement(None, "edge_clipped", centroid)
        if candidate.saturated:
            return TrackedStarMeasurement(None, "saturated", centroid)
        if candidate.hfd_px is None:
            return TrackedStarMeasurement(None, "no_width", centroid)
        return TrackedStarMeasurement(candidate.hfd_px, None, centroid)

    def _follow(self, candidate: StarCandidate, result: TrackedStarMeasurement) -> None:
        if result.centroid is not None and result.reason != "edge_clipped":
            self._target = result.centroid
            self._radius_px = candidate.radius_px

    def acquire(self, frame: np.ndarray) -> TrackedStarMeasurement:
        stats = self._statistics(frame)
        candidates = find_star_candidates(frame, stats=stats, binning=self._binning)
        self._candidate_count = len(candidates)
        if not candidates:
            return TrackedStarMeasurement(None, "no_star")
        height, width = frame.shape
        cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
        ranked = sorted(candidates, key=lambda c: math.hypot(c.x - cx, c.y - cy))
        if len(ranked) > 1 and (
            math.hypot(ranked[1].x - cx, ranked[1].y - cy)
            - math.hypot(ranked[0].x - cx, ranked[0].y - cy)
            < CENTRE_TIE_PX
        ):
            return TrackedStarMeasurement(None, "multiple_candidates")
        result = self._classify(ranked[0], frame.shape)
        # Identity is fixed even if this first sample isn't usable (e.g. saturated): later
        # samples must follow the SAME blob.
        self._follow(ranked[0], result)
        return result

    def measure(self, frame: np.ndarray) -> TrackedStarMeasurement:
        if self._target is None:
            return TrackedStarMeasurement(None, "no_star")
        stats = self._statistics(frame)
        tx, ty = self._target
        max_shift = max(self._max_shift_px, 0.5 * self._radius_px)
        candidates = self._candidates_near(frame, stats, max_shift)
        nearby = sorted(
            (c for c in candidates if math.hypot(c.x - tx, c.y - ty) <= max_shift),
            key=lambda c: math.hypot(c.x - tx, c.y - ty),
        )
        if not nearby:
            return TrackedStarMeasurement(None, "not_found_at_tracked_position")
        if len(nearby) > 1 and (
            math.hypot(nearby[1].x - tx, nearby[1].y - ty)
            - math.hypot(nearby[0].x - tx, nearby[0].y - ty)
            < CENTRE_TIE_PX
        ):
            return TrackedStarMeasurement(None, "multiple_candidates")
        result = self._classify(nearby[0], frame.shape)
        self._follow(nearby[0], result)
        return result

    def _candidates_near(
        self, frame: np.ndarray, stats: FrameStatistics, max_shift: float
    ) -> tuple[StarCandidate, ...]:
        """Issue #58 (F4): the candidates of a crop around the target; again on the whole
        frame when a candidate reaches the crop's border inside the frame."""
        assert self._target is not None and self._binning is not None
        b = self._binning
        half = max_shift + _CROP_RADII * self._radius_px + _CROP_MARGIN_PX
        height, width = frame.shape
        tx, ty = self._target
        # Crop corners on the binning grid, so a crop is binned like the whole frame.
        x0 = max(0, int((tx - half) // b) * b)
        y0 = max(0, int((ty - half) // b) * b)
        x1 = min(width, int(math.ceil((tx + half) / b)) * b)
        y1 = min(height, int(math.ceil((ty + half) / b)) * b)
        candidates = find_star_candidates(
            frame[y0:y1, x0:x1], stats=stats, binning=b, origin=(x0, y0),
            frame_shape=(height, width),
        )
        if any(c.truncated for c in candidates):
            candidates = find_star_candidates(frame, stats=stats, binning=b)
        return candidates
