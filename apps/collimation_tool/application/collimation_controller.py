"""CollimationController — orchestrates rough (donut-based) collimation.

Ported, in spirit, from smart_telescope's `services/collimation/
assistant.py::CollimationAssistant`, but deliberately NOT a background-
thread session runner with its own report/archive/cross-app-guiding
coordination — those are all UI/session concerns this project handles
differently (or defers), per docs/porting-notes.md. This controller
exposes plain synchronous methods that a UI (Stage 7) or test calls
explicitly at each step, plus the exact measure -> advise decision logic
ported from `_handle_auto_exposure` / `_handle_measure_donut`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from astrotool_core.frames.analysis_plane import AnalysisPlane

from collimation_tool.domain.collimation_measurement import (
    DonutAnalysisResult,
    DonutAnalyzer,
    DonutMeasurement,
)
from collimation_tool.domain.collimation_state import (
    CollimationAdvisor,
    CollimationRecommendation,
    CollimationStateMachine,
    ScrewCalibration,
    ScrewResponseLearner,
)

_TARGET_PEAK_FRACTION = 0.80
_EXPOSURE_TOLERANCE = 0.10
_MIN_EXPOSURE_S = 0.001
_MAX_EXPOSURE_S = 30.0
_MIN_EXPOSURE_CHANGE_S = 0.001
_MAX_EXPOSURE_ATTEMPTS = 8


def adjust_exposure(
    plane: AnalysisPlane,
    *,
    current_exposure_s: float,
    target_fraction: float = _TARGET_PEAK_FRACTION,
    tolerance: float = _EXPOSURE_TOLERANCE,
    min_exposure_s: float = _MIN_EXPOSURE_S,
    max_exposure_s: float = _MAX_EXPOSURE_S,
    min_change_s: float = _MIN_EXPOSURE_CHANGE_S,
) -> float | None:
    """One step of exposure auto-adjustment. Returns None once converged.

    Ported from `_handle_auto_exposure`'s per-iteration formula: pushes
    the peak pixel toward ``target_fraction`` of full well.
    """
    max_adu = float(2**plane.bit_depth - 1)
    fraction = float(plane.mono.max()) / max_adu
    if abs(fraction - target_fraction) < tolerance:
        return None
    new_exposure = current_exposure_s * (target_fraction / max(fraction, 0.01))
    new_exposure = max(min_exposure_s, min(max_exposure_s, new_exposure))
    if abs(new_exposure - current_exposure_s) < min_change_s:
        return None
    return new_exposure


def run_auto_exposure(
    capture: Callable[[float], AnalysisPlane],
    *,
    initial_exposure_s: float,
    max_attempts: int = _MAX_EXPOSURE_ATTEMPTS,
    **adjust_kwargs: float,
) -> float:
    """Loop `adjust_exposure` against live captures until converged or exhausted."""
    exposure_s = initial_exposure_s
    for _ in range(max_attempts):
        plane = capture(exposure_s)
        new_exposure = adjust_exposure(plane, current_exposure_s=exposure_s, **adjust_kwargs)
        if new_exposure is None:
            break
        exposure_s = new_exposure
    return exposure_s


@dataclass
class CollimationController:
    """Ties DonutAnalyzer + CollimationAdvisor + ScrewResponseLearner + the
    state machine together for the rough (donut-based) workflow.

    Session-flow orchestration (when to transition the state machine) is
    left to the caller — this class provides the measurement/decision
    steps, not a driving loop.
    """

    state_machine: CollimationStateMachine = field(default_factory=CollimationStateMachine)
    learner: ScrewResponseLearner = field(default_factory=ScrewResponseLearner)
    advisor: CollimationAdvisor = field(default_factory=lambda: CollimationAdvisor([]))
    last_measurement: DonutMeasurement | None = field(default=None, init=False)
    last_recommendation: CollimationRecommendation | None = field(default=None, init=False)

    def measure_and_advise(
        self, plane: AnalysisPlane
    ) -> tuple[DonutAnalysisResult, CollimationRecommendation | None]:
        """Analyze `plane` for a donut and update the current recommendation.

        Ported from `_handle_measure_donut`'s decision shape: a failed
        measurement clears the recommendation rather than raising —
        callers decide how many attempts/frames to retry with.
        """
        result = DonutAnalyzer().analyze(plane)
        if result.measurement is not None:
            self.last_measurement = result.measurement
            self.last_recommendation = self.advisor.recommend(result.measurement)
        else:
            self.last_recommendation = None
        return result, self.last_recommendation

    def record_screw_adjustment(
        self,
        screw_id: str,
        before: DonutMeasurement,
        after: DonutMeasurement,
        turn_cw: bool,
    ) -> ScrewCalibration:
        """Record an observed screw response and refresh the advisor's calibrations."""
        calibration = self.learner.observe(screw_id, before, after, turn_cw)
        self.advisor = CollimationAdvisor(self.learner.get_all())
        return calibration
