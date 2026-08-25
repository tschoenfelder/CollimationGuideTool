"""Guide session/source health tracking.

Ported from smart_telescope's `domain/guiding.py` (`GuideSourceHealth`,
`GuideSourceState`) and `services/guide_measurement.py`
(`source_state_from_measurement`). The multi-camera role-selection concept
(`GuideSourceSelector`, primary/fallback roles across main/guide/oag
cameras) is dropped — this project's GuideTool, like the rest of
astrotool_core (see Stage 3's `StreamController`), assumes a single guide
camera, not smart_telescope's multi-camera setup.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from guide_tool.domain.guide_error import GuideError


class GuideSourceHealth(enum.StrEnum):
    HEALTHY = "healthy"
    TRANSIENT_BAD = "transient_bad"
    HARD_FAILED = "hard_failed"


@dataclass(frozen=True)
class GuideSourceState:
    running: bool
    health: GuideSourceHealth
    latest_sequence: int = 0
    latest_frame_age_s: float | None = None
    bad_frame_count: int = 0
    hard_failure: str | None = None
    error: GuideError | None = None


def source_state_from_error(
    error: GuideError | None,
    *,
    running: bool,
    latest_sequence: int,
    latest_frame_age_s: float | None,
    bad_frame_count: int,
    fallback_after_bad_frames: int,
    hard_failure: str | None = None,
) -> GuideSourceState:
    """Build a GuideSourceState from a guide-error result and stream health counters."""
    if hard_failure is not None:
        health = GuideSourceHealth.HARD_FAILED
    elif bad_frame_count >= fallback_after_bad_frames:
        health = GuideSourceHealth.TRANSIENT_BAD
    else:
        health = GuideSourceHealth.HEALTHY
    return GuideSourceState(
        running=running,
        health=health,
        latest_sequence=latest_sequence,
        latest_frame_age_s=latest_frame_age_s,
        bad_frame_count=bad_frame_count,
        hard_failure=hard_failure,
        error=error,
    )
