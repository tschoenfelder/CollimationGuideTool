import pytest
from collimation_tool.domain.collimation_measurement import CircleEllipseFit, DonutMeasurement
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


def _measurement(error_x: float, error_y: float, *, outer_radius: float = 50.0) -> DonutMeasurement:
    outer = CircleEllipseFit(
        center_x=100.0,
        center_y=100.0,
        radius_x=outer_radius,
        radius_y=outer_radius,
        confidence=0.95,
    )
    inner = CircleEllipseFit(
        center_x=100.0 + error_x,
        center_y=100.0 + error_y,
        radius_x=20.0,
        radius_y=20.0,
        confidence=0.9,
    )
    return DonutMeasurement(
        outer_ring=outer,
        inner_hole=inner,
        error_x_px=error_x,
        error_y_px=error_y,
        error_magnitude_px=(error_x**2 + error_y**2) ** 0.5,
        error_angle_deg=0.0,
        confidence=0.9,
    )


def _cal(
    screw_id: str, x: float, y: float, *, samples: int = 5, confidence: float = 1.0
) -> ScrewCalibration:
    return ScrewCalibration(
        screw_id, response_vector_x=x, response_vector_y=y, samples=samples, confidence=confidence
    )


class TestCollimationRecommendation:
    def test_is_actionable_requires_confidence_and_a_direction(self) -> None:
        rec = CollimationRecommendation(
            "T1", TurnDirection.CLOCKWISE, AdjustmentSize.SMALL, "x", 0.6
        )
        assert rec.is_actionable is True

        low_conf = CollimationRecommendation(
            "T1", TurnDirection.CLOCKWISE, AdjustmentSize.SMALL, "x", 0.4
        )
        assert low_conf.is_actionable is False

        no_direction = CollimationRecommendation(
            "T1", TurnDirection.NONE, AdjustmentSize.SMALL, "x", 0.9
        )
        assert no_direction.is_actionable is False


class TestScrewCalibration:
    def test_response_magnitude(self) -> None:
        cal = _cal("T1", 3.0, 4.0)
        assert cal.response_magnitude == pytest.approx(5.0)


class TestCollimationAdvisor:
    def test_no_calibrations_returns_none(self) -> None:
        advisor = CollimationAdvisor([])
        assert advisor.recommend(_measurement(5.0, 0.0)) is None

    def test_already_collimated_returns_none(self) -> None:
        advisor = CollimationAdvisor([_cal("T1", 10.0, 0.0)])
        assert advisor.recommend(_measurement(0.5, 0.0)) is None

    def test_recommends_the_screw_whose_response_best_opposes_the_error(self) -> None:
        advisor = CollimationAdvisor([_cal("T1", 10.0, 0.0), _cal("T2", 0.0, 10.0)])

        rec = advisor.recommend(_measurement(5.0, 0.0))

        assert rec is not None
        assert rec.screw_id == "T1"
        assert rec.turn_direction is TurnDirection.COUNTER_CLOCKWISE
        assert rec.confidence == pytest.approx(1.0)

    def test_opposite_error_sign_flips_turn_direction(self) -> None:
        advisor = CollimationAdvisor([_cal("T1", 10.0, 0.0)])

        positive_error = advisor.recommend(_measurement(5.0, 0.0))
        negative_error = advisor.recommend(_measurement(-5.0, 0.0))

        assert positive_error is not None
        assert negative_error is not None
        assert positive_error.turn_direction is TurnDirection.COUNTER_CLOCKWISE
        assert negative_error.turn_direction is TurnDirection.CLOCKWISE

    def test_large_error_ratio_yields_medium_adjustment(self) -> None:
        advisor = CollimationAdvisor([_cal("T1", 10.0, 0.0)])
        rec = advisor.recommend(_measurement(20.0, 0.0))  # 40% of 50px radius
        assert rec is not None
        assert rec.adjustment_size is AdjustmentSize.MEDIUM

    def test_small_error_ratio_yields_small_adjustment(self) -> None:
        advisor = CollimationAdvisor([_cal("T1", 10.0, 0.0)])
        rec = advisor.recommend(_measurement(5.0, 0.0))  # 10% of 50px radius
        assert rec is not None
        assert rec.adjustment_size is AdjustmentSize.SMALL

    def test_low_calibration_confidence_halves_confidence_and_notes_recalibration(self) -> None:
        advisor = CollimationAdvisor([_cal("T1", 10.0, 0.0, samples=1, confidence=0.2)])
        rec = advisor.recommend(_measurement(20.0, 0.0))
        assert rec is not None
        assert rec.confidence == pytest.approx(0.1)
        assert "recalibrat" in rec.reason

    def test_screws_with_negligible_response_are_ignored(self) -> None:
        advisor = CollimationAdvisor([_cal("T1", 0.1, 0.0)])
        assert advisor.recommend(_measurement(20.0, 0.0)) is None


class TestScrewResponseLearner:
    def test_observe_computes_cw_equivalent_delta(self) -> None:
        learner = ScrewResponseLearner()
        before = _measurement(0.0, 0.0)
        after = _measurement(10.0, 0.0)
        cal = learner.observe("T1", before, after, turn_cw=True)
        assert cal.response_vector_x == pytest.approx(10.0)
        assert cal.samples == 1

    def test_ccw_observation_is_negated_to_cw_equivalent(self) -> None:
        learner = ScrewResponseLearner()
        before = _measurement(0.0, 0.0)
        after = _measurement(10.0, 0.0)
        cal = learner.observe("T1", before, after, turn_cw=False)
        assert cal.response_vector_x == pytest.approx(-10.0)

    def test_confidence_saturates_at_five_samples(self) -> None:
        learner = ScrewResponseLearner()
        before = _measurement(0.0, 0.0)
        after = _measurement(10.0, 0.0)
        cal = learner.observe("T1", before, after, turn_cw=True)
        for _ in range(6):
            cal = learner.observe("T1", before, after, turn_cw=True)
        assert cal.samples == 7
        assert cal.confidence == 1.0

    def test_averages_multiple_observations(self) -> None:
        learner = ScrewResponseLearner()
        before = _measurement(0.0, 0.0)
        learner.observe("T1", before, _measurement(10.0, 0.0), turn_cw=True)
        learner.observe("T1", before, _measurement(20.0, 0.0), turn_cw=True)
        cal = learner.get_calibration("T1")
        assert cal is not None
        assert cal.response_vector_x == pytest.approx(15.0)
        assert cal.samples == 2

    def test_unknown_screw_returns_none(self) -> None:
        learner = ScrewResponseLearner()
        assert learner.get_calibration("T9") is None

    def test_get_all_returns_every_observed_screw(self) -> None:
        learner = ScrewResponseLearner()
        before = _measurement(0.0, 0.0)
        learner.observe("T1", before, _measurement(10.0, 0.0), turn_cw=True)
        learner.observe("T2", before, _measurement(0.0, 10.0), turn_cw=True)
        screw_ids = {cal.screw_id for cal in learner.get_all()}
        assert screw_ids == {"T1", "T2"}


class TestCollimationStateMachine:
    def _run_to(self, machine: CollimationStateMachine, *states: CollimationState) -> None:
        for state in states:
            machine.transition(state)

    def test_starts_idle(self) -> None:
        machine = CollimationStateMachine()
        assert machine.state is CollimationState.IDLE
        assert machine.is_terminal() is False
        assert machine.is_waiting_for_user() is False

    def test_full_happy_path_reaches_complete(self) -> None:
        machine = CollimationStateMachine()
        self._run_to(
            machine,
            CollimationState.PRECHECK,
            CollimationState.ACQUIRE_STAR,
            CollimationState.CENTER_STAR,
            CollimationState.AUTO_EXPOSURE,
            CollimationState.ROUGH_DEFOCUS,
            CollimationState.MAP_SCREWS_BY_OBSTRUCTION,
            CollimationState.MEASURE_DONUT,
            CollimationState.GUIDE_ROUGH_COLLIMATION,
        )
        assert machine.is_waiting_for_user() is True
        machine.transition(CollimationState.FOCUS)
        machine.transition(CollimationState.COMPLETE)
        assert machine.is_terminal() is True

    def test_guide_rough_collimation_can_loop_back_to_measure_donut(self) -> None:
        machine = CollimationStateMachine()
        self._run_to(
            machine,
            CollimationState.PRECHECK,
            CollimationState.ACQUIRE_STAR,
            CollimationState.CENTER_STAR,
            CollimationState.AUTO_EXPOSURE,
            CollimationState.ROUGH_DEFOCUS,
            CollimationState.MAP_SCREWS_BY_OBSTRUCTION,
            CollimationState.MEASURE_DONUT,
            CollimationState.GUIDE_ROUGH_COLLIMATION,
        )
        machine.transition(CollimationState.MEASURE_DONUT)
        assert machine.state is CollimationState.MEASURE_DONUT
        assert machine.prev_state is CollimationState.GUIDE_ROUGH_COLLIMATION

    def test_invalid_transition_raises(self) -> None:
        machine = CollimationStateMachine()
        with pytest.raises(InvalidTransitionError):
            machine.transition(CollimationState.COMPLETE)

    def test_any_state_can_fail(self) -> None:
        machine = CollimationStateMachine()
        machine.transition(CollimationState.PRECHECK)
        machine.transition(CollimationState.FAILED)
        assert machine.is_terminal() is True

    def test_pause_and_resume_restores_prior_state(self) -> None:
        machine = CollimationStateMachine()
        machine.transition(CollimationState.PRECHECK)
        machine.pause()
        paused_state = machine.state
        assert paused_state is CollimationState.PAUSED
        machine.resume()
        resumed_state = machine.state
        assert resumed_state is CollimationState.PRECHECK

    def test_resume_without_pause_is_a_noop(self) -> None:
        machine = CollimationStateMachine()
        machine.transition(CollimationState.PRECHECK)
        machine.resume()
        assert machine.state is CollimationState.PRECHECK

    def test_reset_returns_to_idle(self) -> None:
        machine = CollimationStateMachine()
        machine.transition(CollimationState.PRECHECK)
        machine.reset()
        assert machine.state is CollimationState.IDLE

    def test_instruction_text_is_available_for_every_state(self) -> None:
        from collimation_tool.domain.collimation_state import STATE_INSTRUCTIONS

        for state in CollimationState:
            assert STATE_INSTRUCTIONS[state] != ""
