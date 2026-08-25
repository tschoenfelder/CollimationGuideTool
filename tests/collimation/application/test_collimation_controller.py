import numpy as np
import pytest
from astrotool_core.frames.analysis_plane import AnalysisPlane, build_analysis_plane
from astrotool_core.testing.frame_factory import donut_image, make_frame
from collimation_tool.application.collimation_controller import (
    CollimationController,
    adjust_exposure,
    run_auto_exposure,
)
from collimation_tool.domain.collimation_state import CollimationAdvisor, ScrewCalibration


def _flat_plane(fraction: float, *, bit_depth: int = 16) -> AnalysisPlane:
    max_adu = float(2**bit_depth - 1)
    pixels = np.full((16, 16), fraction * max_adu, dtype=np.float32)
    return build_analysis_plane(make_frame(pixels, bit_depth=bit_depth))


class TestAdjustExposure:
    def test_dim_frame_increases_exposure(self) -> None:
        new_exposure = adjust_exposure(_flat_plane(0.5), current_exposure_s=1.0)
        assert new_exposure == pytest.approx(1.6, abs=0.01)

    def test_bright_frame_decreases_exposure(self) -> None:
        new_exposure = adjust_exposure(_flat_plane(0.95), current_exposure_s=1.0)
        assert new_exposure is not None
        assert new_exposure < 1.0

    def test_converged_frame_returns_none(self) -> None:
        assert adjust_exposure(_flat_plane(0.80), current_exposure_s=1.0) is None

    def test_result_is_clamped_to_bounds(self) -> None:
        new_exposure = adjust_exposure(
            _flat_plane(0.01), current_exposure_s=1.0, max_exposure_s=2.0
        )
        assert new_exposure == 2.0


def test_run_auto_exposure_converges_within_max_attempts() -> None:
    # Simulate a camera whose peak fraction always tracks exposure exactly:
    # fraction = exposure_s * 0.8, so the target (0.8 fraction at exposure=1.0)
    # is reached in one adjustment step from an initial guess of 0.5s.
    def capture(exposure_s: float) -> AnalysisPlane:
        fraction = min(1.0, exposure_s * 0.8)
        return _flat_plane(fraction)

    final_exposure = run_auto_exposure(capture, initial_exposure_s=0.5)
    assert final_exposure == pytest.approx(1.0, abs=0.05)


def test_run_auto_exposure_stops_at_max_attempts_if_never_converging() -> None:
    calls = {"n": 0}

    def capture(exposure_s: float) -> AnalysisPlane:
        calls["n"] += 1
        return _flat_plane(0.5)  # never near the 0.8 target -> always adjusts

    run_auto_exposure(capture, initial_exposure_s=1.0, max_attempts=3)
    assert calls["n"] == 3


class TestCollimationController:
    def _donut_plane(self, inner_offset: tuple[float, float]) -> AnalysisPlane:
        image = donut_image(
            (240, 240),
            outer_center=(120.0, 120.0),
            outer_radius=50.0,
            inner_center=(120.0 + inner_offset[0], 120.0 + inner_offset[1]),
            inner_radius=20.0,
            peak=3000.0,
        )
        return build_analysis_plane(make_frame(image))

    def test_measure_and_advise_with_no_calibrations_returns_measurement_but_no_recommendation(
        self,
    ) -> None:
        controller = CollimationController()
        result, recommendation = controller.measure_and_advise(self._donut_plane((5.0, 0.0)))
        assert result.reason == "ok"
        assert controller.last_measurement is not None
        assert recommendation is None  # advisor has no calibrations yet

    def test_measure_and_advise_with_calibrations_produces_a_recommendation(self) -> None:
        cal = ScrewCalibration(
            "T1", response_vector_x=10.0, response_vector_y=0.0, samples=5, confidence=1.0
        )
        controller = CollimationController(advisor=CollimationAdvisor([cal]))
        _, recommendation = controller.measure_and_advise(self._donut_plane((5.0, 0.0)))
        assert recommendation is not None
        assert recommendation.screw_id == "T1"

    def test_failed_measurement_clears_the_recommendation(self) -> None:
        cal = ScrewCalibration(
            "T1", response_vector_x=10.0, response_vector_y=0.0, samples=5, confidence=1.0
        )
        controller = CollimationController(advisor=CollimationAdvisor([cal]))
        controller.measure_and_advise(self._donut_plane((5.0, 0.0)))
        assert controller.last_recommendation is not None

        dark_plane = build_analysis_plane(make_frame(np.full((64, 64), 100.0, dtype=np.float32)))
        result, recommendation = controller.measure_and_advise(dark_plane)
        assert result.reason == "no_signal"
        assert recommendation is None
        assert controller.last_recommendation is None

    def test_record_screw_adjustment_updates_the_advisor(self) -> None:
        controller = CollimationController()
        before, _ = controller.measure_and_advise(self._donut_plane((0.0, 0.0)))
        after_plane = self._donut_plane((10.0, 0.0))
        after_result, _ = controller.measure_and_advise(after_plane)
        assert before.measurement is not None
        assert after_result.measurement is not None

        cal = controller.record_screw_adjustment(
            "T1", before.measurement, after_result.measurement, turn_cw=True
        )
        assert cal.screw_id == "T1"
        assert cal.samples == 1
        assert controller.advisor.recommend(after_result.measurement) is not None
