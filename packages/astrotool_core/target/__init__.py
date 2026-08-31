"""Point-source detection and single-target ROI tracking.

Covers lock/lost/searching/reacquired state. Never imports
astrotool_core.mount.
"""

from astrotool_core.target.detector import DetectionResult, detect_sources
from astrotool_core.target.point_source import PointSource
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker, TrackingResult, TrackingState
from astrotool_core.target.translation_offset import TranslationOffset, measure_translation_offset

__all__ = [
    "DetectionResult",
    "PointSource",
    "RoiTracker",
    "TrackingResult",
    "TrackingState",
    "TranslationOffset",
    "detect_sources",
    "measure_translation_offset",
    "select_target",
]
