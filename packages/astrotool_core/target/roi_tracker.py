"""RoiTracker — single-locked-target lock-state machine.

New: no full existing analog anywhere. smarttscope_live_analysis.temporal's
``track_sources``/``classify_temporal_tracks`` solve a different problem
(batch multi-frame linking + persistent/transient classification across a
whole star field, for exposure/gain recommendations) — not real-time
nearest-neighbor reacquisition of one already-locked target. This tracker
instead matches each new frame's sources directly against the last known
position, which is the smaller, purpose-built primitive real-time ROI
tracking actually needs. See docs/porting-notes.md Stage 3.

Never imports astrotool_core.mount — this module only ever reports a
measured deviation; it never moves anything.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, auto

from astrotool_core.target.point_source import PointSource


class TrackingState(Enum):
    INITIALIZING = auto()
    LOCKED = auto()
    LOST = auto()
    SEARCHING = auto()
    REACQUIRED = auto()


@dataclass(frozen=True)
class TrackingResult:
    state: TrackingState
    x: float | None
    y: float | None
    matched_source: PointSource | None


class RoiTracker:
    """Tracks one previously-acquired target across frames.

    Call ``acquire(x, y)`` once (typically with the position
    ``roi_selector.select_target`` picked from the first frame), then
    ``update(sources)`` once per subsequent frame's detection result.
    """

    def __init__(
        self,
        *,
        lock_tolerance_px: float = 8.0,
        search_radius_px: float = 40.0,
        lost_to_searching_frames: int = 1,
    ) -> None:
        self._state = TrackingState.INITIALIZING
        self._x: float | None = None
        self._y: float | None = None
        self._lost_streak = 0
        self._lock_tolerance_px = lock_tolerance_px
        self._search_radius_px = search_radius_px
        self._lost_to_searching_frames = lost_to_searching_frames

    @property
    def state(self) -> TrackingState:
        return self._state

    def acquire(self, x: float, y: float) -> TrackingResult:
        """Seed the tracker at (x, y) and enter LOCKED."""
        self._x, self._y = x, y
        self._lost_streak = 0
        self._state = TrackingState.LOCKED
        return TrackingResult(state=self._state, x=self._x, y=self._y, matched_source=None)

    def update(self, sources: Iterable[PointSource]) -> TrackingResult:
        """Advance the tracker one frame given this frame's detected sources."""
        if self._state is TrackingState.INITIALIZING or self._x is None or self._y is None:
            raise RuntimeError("RoiTracker.update() called before acquire()")

        searching = self._state in (TrackingState.LOST, TrackingState.SEARCHING)
        radius = self._search_radius_px if searching else self._lock_tolerance_px
        match = _nearest_within(sources, self._x, self._y, radius)

        if match is not None:
            self._x, self._y = match.x, match.y
            self._lost_streak = 0
            self._state = TrackingState.REACQUIRED if searching else TrackingState.LOCKED
            return TrackingResult(state=self._state, x=self._x, y=self._y, matched_source=match)

        if self._state in (TrackingState.LOCKED, TrackingState.REACQUIRED):
            self._state = TrackingState.LOST
            self._lost_streak = 1
        elif self._state is TrackingState.LOST:
            self._lost_streak += 1
            if self._lost_streak > self._lost_to_searching_frames:
                self._state = TrackingState.SEARCHING
        # SEARCHING with no match: stays SEARCHING.
        return TrackingResult(state=self._state, x=self._x, y=self._y, matched_source=None)


def _nearest_within(
    sources: Iterable[PointSource],
    x: float,
    y: float,
    radius_px: float,
) -> PointSource | None:
    best: PointSource | None = None
    best_distance2 = radius_px * radius_px
    for source in sources:
        distance2 = (source.x - x) ** 2 + (source.y - y) ** 2
        if distance2 <= best_distance2:
            best = source
            best_distance2 = distance2
    return best
