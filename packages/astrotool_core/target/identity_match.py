"""Identity-preserving candidate resolution — issue #15 AC 1.6: prefer
the candidate consistent with the predicted target position and previous
target properties (brightness), and never silently switch to a different
star when identity is ambiguous.

Sits in front of `RoiTracker`/`select_target` — neither has any ambiguity
concept of its own (`RoiTracker._nearest_within` always silently picks
whichever candidate is closest; `select_target` always picks whichever is
brightest). A caller resolves identity here FIRST; only a confident
`MATCHED` result should ever be handed to `RoiTracker.update()`/
`acquire()`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

from astrotool_core.target.point_source import PointSource

Point = tuple[float, float]

#: A second-best candidate whose own score is at least this fraction of
#: the best candidate's own score is treated as genuinely ambiguous
#: evidence, not resolved by picking the (barely) higher-scoring one --
#: same "second-place-score-ratio" convention
#: `terrestrial_registrar._AMBIGUITY_SCORE_RATIO` already established in
#: this codebase for registration matches, applied here to star
#: candidates instead.
_DEFAULT_AMBIGUITY_SCORE_RATIO = 0.85

_DEFAULT_MAX_POSITION_ERROR_PX = 25.0


class IdentityMatchStatus(Enum):
    MATCHED = "matched"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class IdentityMatchResult:
    status: IdentityMatchStatus
    source: PointSource | None


def _position_score(source: PointSource, predicted_position: Point, max_error_px: float) -> float:
    distance = math.hypot(source.x - predicted_position[0], source.y - predicted_position[1])
    return max(0.0, 1.0 - distance / max_error_px)


def _brightness_score(source: PointSource, expected_peak: float | None) -> float:
    if expected_peak is None or expected_peak <= 0.0 or source.peak <= 0.0:
        return 1.0
    return min(source.peak, expected_peak) / max(source.peak, expected_peak)


def resolve_identity(
    candidates: Sequence[PointSource],
    *,
    predicted_position: Point,
    expected_peak: float | None = None,
    max_position_error_px: float = _DEFAULT_MAX_POSITION_ERROR_PX,
    ambiguity_score_ratio: float = _DEFAULT_AMBIGUITY_SCORE_RATIO,
) -> IdentityMatchResult:
    in_range = [
        source
        for source in candidates
        if math.hypot(
            source.x - predicted_position[0], source.y - predicted_position[1]
        )
        <= max_position_error_px
    ]
    if not in_range:
        return IdentityMatchResult(status=IdentityMatchStatus.NOT_FOUND, source=None)

    scored = sorted(
        (
            (
                _position_score(source, predicted_position, max_position_error_px)
                * _brightness_score(source, expected_peak),
                source,
            )
            for source in in_range
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )

    best_score, best_source = scored[0]
    if len(scored) > 1:
        second_score, _second_source = scored[1]
        if best_score > 0.0 and second_score >= best_score * ambiguity_score_ratio:
            return IdentityMatchResult(status=IdentityMatchStatus.AMBIGUOUS, source=None)

    return IdentityMatchResult(status=IdentityMatchStatus.MATCHED, source=best_source)
