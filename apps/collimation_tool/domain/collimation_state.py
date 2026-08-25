"""Collimation recommendation model, screw-response learning, and the
rough-collimation session state machine.

Ported from smart_telescope's `domain/collimation/models.py` (enums,
`CollimationRecommendation`, `ScrewCalibration`), `services/collimation/
collimation_advisor.py` (`CollimationAdvisor`), `services/collimation/
screw_mapper.py` (`ScrewResponseLearner`, renamed from the source's bare
dataclass name only in that this module documents it alongside its
sibling types), and a deliberately trimmed subset of `services/
collimation/state_machine.py`'s 20-state FSM.

Scope note (Stage 5): the state machine here covers only the rough
(donut-based) collimation + focus workflow. Dropped entirely: SELECT_STAR/
SLEW_TO_STAR (the new MountPort has no goto), and the whole Tri-Bahtinov
mask fine-collimation branch (INSTALL_TRIBAHTINOV, MAP_MASK_SECTORS,
FINE_FOCUS, MEASURE_SPIKES, GUIDE_FINE_COLLIMATION, MASKLESS_VALIDATION) —
deferred to a later stage, see docs/porting-notes.md.
"""

from __future__ import annotations

import enum
import math
import threading
from dataclasses import dataclass

from collimation_tool.domain.collimation_measurement import DonutMeasurement

# ── Enums and recommendation/calibration models ──────────────────────────


class TurnDirection(enum.StrEnum):
    CLOCKWISE = "clockwise"
    COUNTER_CLOCKWISE = "counter_clockwise"
    NONE = "none"


class AdjustmentSize(enum.StrEnum):
    LARGE = "large"  # > 1/4 turn
    MEDIUM = "medium"  # 1/8-1/4 turn
    SMALL = "small"  # < 1/8 turn
    NONE = "none"


@dataclass(frozen=True)
class CollimationRecommendation:
    """One actionable screw-turn recommendation."""

    screw_id: str  # "T1", "T2", "T3" (SCT tilt screws)
    turn_direction: TurnDirection
    adjustment_size: AdjustmentSize
    reason: str
    confidence: float  # 0-1; below 0.5 -> display warning only

    @property
    def is_actionable(self) -> bool:
        """True when confidence is high enough to show as a command (not just a hint)."""
        return self.confidence >= 0.5 and self.turn_direction != TurnDirection.NONE


@dataclass(frozen=True)
class ScrewCalibration:
    """Learned response of one collimation screw.

    Populated by observing how a unit CW turn shifts the donut center.
    """

    screw_id: str
    response_vector_x: float  # px shift per small CW turn (image x-axis)
    response_vector_y: float  # px shift per small CW turn (image y-axis)
    samples: int
    confidence: float  # 0-1; increases with samples

    @property
    def response_magnitude(self) -> float:
        return math.hypot(self.response_vector_x, self.response_vector_y)


# ── CollimationAdvisor (ported from services/collimation/collimation_advisor.py) ─

_COLLIMATED_FRACTION = 0.02
_MEDIUM_FRACTION = 0.15
_RECAL_CONFIDENCE = 0.30
_MIN_RESPONSE_PX = 0.5


class CollimationAdvisor:
    """Picks the screw + turn direction that best opposes a measured donut error."""

    def __init__(
        self,
        calibrations: list[ScrewCalibration],
        collimated_fraction: float = _COLLIMATED_FRACTION,
        recal_confidence: float = _RECAL_CONFIDENCE,
    ) -> None:
        self._calibrations = [c for c in calibrations if c.response_magnitude >= _MIN_RESPONSE_PX]
        self._collimated_fraction = collimated_fraction
        self._recal_confidence = recal_confidence

    def recommend(
        self,
        measurement: DonutMeasurement,
        outer_radius: float | None = None,
    ) -> CollimationRecommendation | None:
        if not self._calibrations:
            return None

        r_outer = max(outer_radius or measurement.outer_ring.mean_radius, 1.0)
        error_x, error_y = measurement.error_x_px, measurement.error_y_px
        error_mag = measurement.error_magnitude_px
        if error_mag < self._collimated_fraction * r_outer:
            return None

        corr_x, corr_y = -error_x, -error_y

        best_screw: ScrewCalibration | None = None
        best_dot = 0.0
        for cal in self._calibrations:
            dot = corr_x * cal.response_vector_x + corr_y * cal.response_vector_y
            contrib = abs(dot)
            if best_screw is None or contrib > abs(best_dot):
                best_screw = cal
                best_dot = dot
        if best_screw is None:
            return None
        best_align = abs(best_dot) / (error_mag * best_screw.response_magnitude)

        turn_direction = (
            TurnDirection.CLOCKWISE if best_dot > 0 else TurnDirection.COUNTER_CLOCKWISE
        )

        ratio = error_mag / r_outer
        adjustment_size = (
            AdjustmentSize.MEDIUM if ratio > _MEDIUM_FRACTION else AdjustmentSize.SMALL
        )

        confidence = best_align * best_screw.confidence
        reason = (
            f"error {error_mag:.1f} px ({ratio:.1%} of ring radius); "
            f"screw alignment {best_align:.0%}"
        )
        if best_screw.confidence < self._recal_confidence:
            confidence *= 0.5
            reason += "; low calibration confidence — consider recalibrating"

        return CollimationRecommendation(
            screw_id=best_screw.screw_id,
            turn_direction=turn_direction,
            adjustment_size=adjustment_size,
            reason=reason,
            confidence=min(1.0, confidence),
        )


# ── ScrewResponseLearner (ported from services/collimation/screw_mapper.py) ─


class ScrewResponseLearner:
    """Learns each screw's response vector from before/after donut measurement pairs."""

    _CONF_SATURATION_SAMPLES = 5

    def __init__(self) -> None:
        self._observations: dict[str, list[tuple[float, float]]] = {}

    def observe(
        self,
        screw_id: str,
        before: DonutMeasurement,
        after: DonutMeasurement,
        turn_cw: bool,
    ) -> ScrewCalibration:
        dx = after.error_x_px - before.error_x_px
        dy = after.error_y_px - before.error_y_px
        if not turn_cw:
            dx, dy = -dx, -dy
        self._observations.setdefault(screw_id, []).append((dx, dy))
        return self._build_calibration(screw_id)

    def get_calibration(self, screw_id: str) -> ScrewCalibration | None:
        if screw_id not in self._observations:
            return None
        return self._build_calibration(screw_id)

    def get_all(self) -> list[ScrewCalibration]:
        return [self._build_calibration(screw_id) for screw_id in self._observations]

    def _build_calibration(self, screw_id: str) -> ScrewCalibration:
        observations = self._observations[screw_id]
        n = len(observations)
        avg_x = sum(dx for dx, _ in observations) / n
        avg_y = sum(dy for _, dy in observations) / n
        confidence = min(1.0, n / self._CONF_SATURATION_SAMPLES)
        return ScrewCalibration(
            screw_id=screw_id,
            response_vector_x=avg_x,
            response_vector_y=avg_y,
            samples=n,
            confidence=confidence,
        )


# ── Rough-collimation session state machine (trimmed from state_machine.py) ─


class CollimationState(enum.StrEnum):
    IDLE = "idle"
    PAUSED = "paused"
    PRECHECK = "precheck"
    ACQUIRE_STAR = "acquire_star"
    CENTER_STAR = "center_star"
    AUTO_EXPOSURE = "auto_exposure"
    ROUGH_DEFOCUS = "rough_defocus"
    MAP_SCREWS_BY_OBSTRUCTION = "map_screws_by_obstruction"
    MEASURE_DONUT = "measure_donut"
    GUIDE_ROUGH_COLLIMATION = "guide_rough_collimation"
    FOCUS = "focus"
    COMPLETE = "complete"
    FAILED = "failed"


VALID_TRANSITIONS: dict[CollimationState, frozenset[CollimationState]] = {
    CollimationState.IDLE: frozenset({CollimationState.PRECHECK}),
    CollimationState.PRECHECK: frozenset(
        {CollimationState.ACQUIRE_STAR, CollimationState.FAILED}
    ),
    CollimationState.ACQUIRE_STAR: frozenset(
        {CollimationState.CENTER_STAR, CollimationState.FAILED}
    ),
    CollimationState.CENTER_STAR: frozenset(
        {CollimationState.AUTO_EXPOSURE, CollimationState.FAILED}
    ),
    CollimationState.AUTO_EXPOSURE: frozenset(
        {CollimationState.ROUGH_DEFOCUS, CollimationState.FAILED}
    ),
    CollimationState.ROUGH_DEFOCUS: frozenset(
        {CollimationState.MAP_SCREWS_BY_OBSTRUCTION, CollimationState.FAILED}
    ),
    CollimationState.MAP_SCREWS_BY_OBSTRUCTION: frozenset(
        {CollimationState.MEASURE_DONUT, CollimationState.FAILED}
    ),
    CollimationState.MEASURE_DONUT: frozenset(
        {CollimationState.GUIDE_ROUGH_COLLIMATION, CollimationState.FAILED}
    ),
    CollimationState.GUIDE_ROUGH_COLLIMATION: frozenset(
        {CollimationState.MEASURE_DONUT, CollimationState.FOCUS, CollimationState.FAILED}
    ),
    CollimationState.FOCUS: frozenset({CollimationState.COMPLETE, CollimationState.FAILED}),
    CollimationState.COMPLETE: frozenset(),
    CollimationState.FAILED: frozenset(),
}

USER_WAIT_STATES = frozenset({CollimationState.GUIDE_ROUGH_COLLIMATION})
TERMINAL_STATES = frozenset({CollimationState.COMPLETE, CollimationState.FAILED})

STATE_INSTRUCTIONS: dict[CollimationState, str] = {
    CollimationState.IDLE: "Ready to start collimation.",
    CollimationState.PAUSED: "Paused.",
    CollimationState.PRECHECK: "Checking camera and mount connectivity.",
    CollimationState.ACQUIRE_STAR: "Point the telescope at a bright star.",
    CollimationState.CENTER_STAR: "Centering the star in the frame.",
    CollimationState.AUTO_EXPOSURE: "Adjusting exposure for a clear star image.",
    CollimationState.ROUGH_DEFOCUS: "Defocusing to reveal the donut pattern.",
    CollimationState.MAP_SCREWS_BY_OBSTRUCTION: "Touch each collimation screw briefly to map it.",
    CollimationState.MEASURE_DONUT: "Measuring the donut pattern.",
    CollimationState.GUIDE_ROUGH_COLLIMATION: "Turn the recommended screw, then confirm.",
    CollimationState.FOCUS: "Refining focus.",
    CollimationState.COMPLETE: "Collimation complete.",
    CollimationState.FAILED: "Collimation failed.",
}


class InvalidTransitionError(RuntimeError):
    pass


class CollimationStateMachine:
    """Thread-safe explicit FSM driving the rough-collimation + focus workflow."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = CollimationState.IDLE
        self._prev_state: CollimationState = CollimationState.IDLE
        self._pre_pause_state: CollimationState | None = None

    @property
    def state(self) -> CollimationState:
        with self._lock:
            return self._state

    @property
    def prev_state(self) -> CollimationState:
        with self._lock:
            return self._prev_state

    def transition(self, target: CollimationState) -> None:
        with self._lock:
            allowed = VALID_TRANSITIONS.get(self._state, frozenset())
            if target not in allowed:
                raise InvalidTransitionError(f"{self._state.value} -> {target.value} not allowed")
            self._prev_state = self._state
            self._state = target

    def pause(self) -> None:
        with self._lock:
            if self._state == CollimationState.PAUSED:
                return
            self._pre_pause_state = self._state
            self._prev_state = self._state
            self._state = CollimationState.PAUSED

    def resume(self) -> None:
        with self._lock:
            if self._state != CollimationState.PAUSED or self._pre_pause_state is None:
                return
            self._state = self._pre_pause_state
            self._pre_pause_state = None

    def reset(self) -> None:
        with self._lock:
            self._prev_state = self._state
            self._state = CollimationState.IDLE
            self._pre_pause_state = None

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def is_waiting_for_user(self) -> bool:
        return self.state in USER_WAIT_STATES

    def instruction(self) -> str:
        return STATE_INSTRUCTIONS.get(self.state, "")
