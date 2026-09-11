"""Point-source detection and single-target ROI tracking.

Covers lock/lost/searching/reacquired state. Never imports
astrotool_core.mount.
"""

from astrotool_core.target.detector import DetectionResult, detect_sources
from astrotool_core.target.point_source import PointSource
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker, TrackingResult, TrackingState
from astrotool_core.target.translation_offset import (
    TranslationOffset,
    max_unaliased_shift_px,
    measure_translation_offset,
    measure_translation_offset_with_tier,
)

__all__ = [
    "DetectionResult",
    "PointSource",
    "RoiTracker",
    "TrackingResult",
    "TrackingState",
    "TranslationOffset",
    "detect_sources",
    "max_unaliased_shift_px",
    "measure_translation_offset",
    "measure_translation_offset_with_tier",
    "select_target",
]
