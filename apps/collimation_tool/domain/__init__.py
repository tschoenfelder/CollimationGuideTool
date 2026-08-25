"""Collimation-specific domain logic.

Measurement, symmetry/diffraction analysis, focus metric, state model.
No UI, no direct hardware access.
"""

from collimation_tool.domain.collimation_measurement import (
    CircleEllipseFit,
    DonutAnalysisResult,
    DonutAnalyzer,
    DonutMeasurement,
    Point2D,
    ReferenceCenterCalibration,
)
from collimation_tool.domain.collimation_state import (
    AdjustmentSize,
    CollimationAdvisor,
    CollimationRecommendation,
    CollimationState,
    CollimationStateMachine,
    InvalidTransitionError,
    ScrewCalibration,
    ScrewResponseLearner,
    TurnDirection,
)
from collimation_tool.domain.focus_metric import FocusQuality, classify_focus_quality, mean_fwhm_px
from collimation_tool.domain.symmetry_analysis import ObstructionResult, detect_obstruction

__all__ = [
    "AdjustmentSize",
    "CircleEllipseFit",
    "CollimationAdvisor",
    "CollimationRecommendation",
    "CollimationState",
    "CollimationStateMachine",
    "DonutAnalysisResult",
    "DonutAnalyzer",
    "DonutMeasurement",
    "FocusQuality",
    "InvalidTransitionError",
    "ObstructionResult",
    "Point2D",
    "ReferenceCenterCalibration",
    "ScrewCalibration",
    "ScrewResponseLearner",
    "TurnDirection",
    "classify_focus_quality",
    "detect_obstruction",
    "mean_fwhm_px",
]
