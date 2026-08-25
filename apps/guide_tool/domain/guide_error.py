"""GuideError — the guiding-specific error signal, derived from a
RoiTracker TrackingResult against an established guide target.

Ported from smart_telescope's `domain/guiding.py::GuideMeasurement`, pared
down: fields that duplicated what `RoiTracker.TrackingResult`/`PointSource`
already carry (the raw pixel centroid measurement, peak/background/noise/
saturated/fwhm_px) are dropped — this module only computes the *error*
relative to an established target, on top of the tracker's own position.

`GuideCentroidEstimator` (the windowed pixel-level centroid/SNR/saturation
measurement in `services/guide_measurement.py`) is not ported at all: it
duplicates `astrotool_core.target.detect_sources`, the same redundancy
already confirmed and avoided for `star_detection.py` in Stage 1/5.
"""

from __future__ import annotations

from dataclasses import dataclass

from astrotool_core.target.roi_tracker import TrackingResult, TrackingState

_LOCKED_STATES = (TrackingState.LOCKED, TrackingState.REACQUIRED)


@dataclass(frozen=True)
class GuideError:
    accepted: bool
    centroid_x: float | None = None
    centroid_y: float | None = None
    target_x: float | None = None
    target_y: float | None = None
    error_x: float | None = None
    error_y: float | None = None
    error_magnitude_px: float | None = None
    rejected_reason: str | None = None


def compute_guide_error(
    result: TrackingResult,
    target: tuple[float, float] | None,
) -> GuideError:
    """Compute the guide error from a tracker result and the current target.

    ``target`` is ``None`` before a target has been established (e.g. the
    very first accepted frame of a session) — the caller adopts the first
    accepted position as the target rather than this function.
    """
    if result.state not in _LOCKED_STATES or result.x is None or result.y is None:
        return GuideError(accepted=False, rejected_reason="star_lost")

    if target is None:
        return GuideError(accepted=True, centroid_x=result.x, centroid_y=result.y)

    error_x = result.x - target[0]
    error_y = result.y - target[1]
    magnitude = (error_x**2 + error_y**2) ** 0.5
    return GuideError(
        accepted=True,
        centroid_x=result.x,
        centroid_y=result.y,
        target_x=target[0],
        target_y=target[1],
        error_x=error_x,
        error_y=error_y,
        error_magnitude_px=magnitude,
    )
