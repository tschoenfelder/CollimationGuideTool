"""Mount Align failure classification (S6.6a, moved verbatim out of the panel).

Which independent layer a failed calibration/nudge belongs to (issue #27 "Failure semantics"),
and the issue #43 "the image did not move" floor.
"""

from __future__ import annotations

from enum import Enum

import numpy as np
from astrotool_core.acquisition import MotionAwareFrameResult, MotionAwareStatus
from astrotool_core.mount import AxisResponse

from collimation_tool.application.mount_align_measurement import Measurement


class MeasurementFailureClass(Enum):
    """Issue #27's "Failure semantics" section: a failed calibration/nudge
    must make it unambiguous which of the three independent layers is at
    fault -- see `MountTestMovePanel.diagnostic_context()`'s own
    `last_failure_classes`, keyed the same way as `_calibration_failed_cameras`
    (per camera, since one camera's frames can be fine while the other's
    aren't)."""

    #: No trustworthy stable frame was obtained at all (see
    #: FrameAcquisitionStatus for the acquisition-layer detail).
    CAPTURE_INVALID = "capture_invalid"
    #: Valid frames were obtained, but the translation estimator
    #: (`measure_translation_offset`) found no trustworthy match.
    MATCH_FAILED = "match_failed"
    #: A valid displacement was measured, but the resulting AXIS1/AXIS2
    #: pair is too close to parallel to invert reliably (`is_degenerate`).
    CALIBRATION_INVALID = "calibration_invalid"
    #: Issue #30: the mount's own tracking state didn't match (and
    #: couldn't be repaired into) what the current target mode requires
    #: (star = ON, terrestrial = OFF) -- a mount-state precondition
    #: failure, not a frame-capture or measurement one.
    TRACKING_STATE_INVALID = "tracking_state_invalid"
    #: Issue #30: frames were obtained (unlike CAPTURE_INVALID), but a
    #: full `stability_sample_count`-frame window never measured within
    #: `stability_tolerance_px` of itself before the deadline -- a
    #: genuinely distinct layer from "never got a frame at all" (e.g.
    #: wind/vibration kept the image moving, not a streaming/connection
    #: problem).
    IMAGE_NOT_STABLE = "image_not_stable"
    #: Issue #46: even the longest allowed calibration move (the normal 3 s
    #: envelope at the configured rate) shifts this camera's image by less
    #: than a measurable fraction of its frame -- reported explicitly rather
    #: than silently extending the move or accepting a tiny displacement.
    MOVE_EXCEEDS_ENVELOPE = "move_exceeds_envelope"
    #: Issue #46: the move overshoots even at the minimum useful duration.
    MOVE_TOO_LARGE = "move_too_large"
    #: Issue #43: stable frames, a confident match -- but the image did not move at all
    #: for a commanded move (the field bundle fe91004f: every response exactly (0, 0)).
    #: Distinct from IMAGE_NOT_STABLE / CAPTURE_INVALID (bad frames) and from a degenerate
    #: axis pair: the frames were fine, the mount produced no measurable image motion.
    NO_MOTION_DETECTED = "no_motion_detected"


#: Issue #43: a measured shift below this is 'the image did not move'.
NO_MOTION_MIN_PX = 1.0
NO_MOTION_WIDTH_FRACTION = 0.0025


def capture_failure_class(result: MotionAwareFrameResult) -> MeasurementFailureClass:
    """A failed capture's layer: no trustworthy frame at all (`CAPTURE_INVALID`) versus frames
    that could not be verified stable (everything else -- issue #30)."""
    return (
        MeasurementFailureClass.CAPTURE_INVALID
        if result.status is MotionAwareStatus.CAPTURE_INVALID
        else MeasurementFailureClass.IMAGE_NOT_STABLE
    )


def bounded_failure_class(reason: str | None) -> MeasurementFailureClass:
    """Issue #46: a probe the sizing policy could not bring into band -- it overshoots even at
    the minimum useful duration, or it stays too small within the envelope."""
    return (
        MeasurementFailureClass.MOVE_TOO_LARGE
        if reason == "overshoot_at_minimum"
        else MeasurementFailureClass.MOVE_EXCEEDS_ENVELOPE
    )


def is_no_motion(response: AxisResponse, measurement: Measurement) -> bool:
    """Issue #43: 'the image did not move' -- the measured shift is below 1 px, or (for a whole
    frame) below `NO_MOTION_WIDTH_FRACTION` of its width."""
    floor = NO_MOTION_MIN_PX
    if isinstance(measurement, np.ndarray):
        floor = max(floor, NO_MOTION_WIDTH_FRACTION * measurement.shape[1])
    return response.magnitude_px < floor
