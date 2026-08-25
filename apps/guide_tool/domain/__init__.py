"""Guiding-specific domain logic.

Guide error, drift estimation, correction model, guiding state. No UI,
no direct hardware access.
"""

from guide_tool.domain.correction_model import (
    GuideCorrectionConfig,
    WouldGuidePulse,
    compute_would_pulses,
)
from guide_tool.domain.drift_estimator import DriftEstimator
from guide_tool.domain.guide_error import GuideError, compute_guide_error
from guide_tool.domain.guiding_state import (
    GuideSourceHealth,
    GuideSourceState,
    source_state_from_error,
)

__all__ = [
    "DriftEstimator",
    "GuideCorrectionConfig",
    "GuideError",
    "GuideSourceHealth",
    "GuideSourceState",
    "WouldGuidePulse",
    "compute_guide_error",
    "compute_would_pulses",
    "source_state_from_error",
]
