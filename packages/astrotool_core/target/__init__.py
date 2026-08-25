"""Point-source detection and single-target ROI tracking.

Covers lock/lost/searching/reacquired state. Never imports
astrotool_core.mount.
"""

from astrotool_core.target.detector import DetectionResult, detect_sources
from astrotool_core.target.point_source import PointSource

__all__ = [
    "DetectionResult",
    "PointSource",
    "detect_sources",
]
