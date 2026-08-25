"""Frame representation, pixel-format/demosaic handling, and the 2D
analysis plane derived from a captured frame.
"""

from astrotool_core.frames.analysis_plane import AnalysisPlane, build_analysis_plane
from astrotool_core.frames.frame import Frame
from astrotool_core.frames.pixel_format import (
    BayerPattern,
    demosaic,
    is_bayer,
    mosaic_from_rgb,
)

__all__ = [
    "AnalysisPlane",
    "BayerPattern",
    "Frame",
    "build_analysis_plane",
    "demosaic",
    "is_bayer",
    "mosaic_from_rgb",
]
