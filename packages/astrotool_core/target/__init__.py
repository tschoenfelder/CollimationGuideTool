"""Point-source detection and single-target ROI tracking.

Covers lock/lost/searching/reacquired state. Never imports
astrotool_core.mount.
"""

from astrotool_core.target.detector import DetectionResult, detect_sources
from astrotool_core.target.frame_registration import (
    FrameRegistrationOutcome,
    FrameRegistrationResult,
    register_frames,
    shift_image,
)
from astrotool_core.target.identity_match import (
    IdentityMatchResult,
    IdentityMatchStatus,
    resolve_identity,
)
from astrotool_core.target.point_source import PointSource
from astrotool_core.target.roi import Roi, compute_roi_bounds, crop_to_roi
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker, TrackingResult, TrackingState
from astrotool_core.target.stacking import RollingFrameBuffer, StackResult, score_frame_quality
from astrotool_core.target.translation_offset import (
    TranslationOffset,
    max_unaliased_shift_px,
    measure_translation_offset,
    measure_translation_offset_with_tier,
)

__all__ = [
    "DetectionResult",
    "FrameRegistrationOutcome",
    "FrameRegistrationResult",
    "IdentityMatchResult",
    "IdentityMatchStatus",
    "PointSource",
    "Roi",
    "RoiTracker",
    "RollingFrameBuffer",
    "StackResult",
    "TrackingResult",
    "TrackingState",
    "TranslationOffset",
    "compute_roi_bounds",
    "crop_to_roi",
    "detect_sources",
    "max_unaliased_shift_px",
    "measure_translation_offset",
    "measure_translation_offset_with_tier",
    "register_frames",
    "resolve_identity",
    "score_frame_quality",
    "select_target",
    "shift_image",
]
