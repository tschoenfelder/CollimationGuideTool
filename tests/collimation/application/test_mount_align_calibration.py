"""S6.6a: Mount Align per-run calibration results (`CalibrationResults`), moved out of
`MountTestMovePanel` unchanged. Tested directly (no panel, no MainWindow, no Qt)."""

from __future__ import annotations

import pytest
from astrotool_core.mount import AxisDirection, AxisResponse, MountAxis
from collimation_tool.application.mount_align_calibration import CalibrationResults
from collimation_tool.application.mount_align_failures import MeasurementFailureClass
from collimation_tool.application.mount_align_measurement import Measurement
from collimation_tool.application.mount_align_plan import CALIBRATION_STEPS, CalibrationStep

pytestmark = pytest.mark.unit

A1, A2 = MountAxis.AXIS1, MountAxis.AXIS2
POS, NEG = AxisDirection.POSITIVE, AxisDirection.NEGATIVE

#: Image shift per (axis, direction): RA moves +x, Dec moves +y.
_GOOD = {
    (A1, POS): (20.0, 0.0),
    (A1, NEG): (-20.0, 0.0),
    (A2, POS): (0.0, 20.0),
    (A2, NEG): (0.0, -20.0),
}


def _response(step: CalibrationStep, shift: tuple[float, float]) -> AxisResponse:
    return AxisResponse(step.axis, step.direction, 1000, shift[0], shift[1], 0.02)


def _run(results: CalibrationResults, shifts: dict[str, dict]) -> None:  # type: ignore[type-arg]
    for step in CALIBRATION_STEPS:
        for key, per_direction in shifts.items():
            results.record_direction_response(
                key, step, _response(step, per_direction[(step.axis, step.direction)])
            )


def test_first_and_repeat_pair_into_a_characterization() -> None:
    results = CalibrationResults()
    first, repeat = CALIBRATION_STEPS[0], CALIBRATION_STEPS[1]
    results.record_direction_response("left", first, _response(first, (12.0, 0.0)))
    assert results.characterizations["left"] == {} and results.partial["left"] == {}
    results.record_direction_response("left", repeat, _response(repeat, (20.0, 0.0)))
    characterization = results.characterizations["left"][(A1, POS)]
    assert characterization.backlash_deficit_px == pytest.approx(8.0)
    assert results.partial["left"][A1].dx_px == 20.0  # the POSITIVE steady state
    negative_repeat = CALIBRATION_STEPS[3]
    results.record_direction_response("left", negative_repeat, _response(negative_repeat, (-1, 0)))
    assert (A1, NEG) not in results.characterizations["left"]  # no FIRST: ignored


def test_measure_step_excludes_cameras_without_frames_or_match() -> None:
    results = CalibrationResults()
    step = CALIBRATION_STEPS[0]
    before: dict[str, Measurement] = {"left": (1.0, 1.0), "right": (2.0, 2.0)}
    after: dict[str, Measurement] = {"left": (5.0, 1.0)}
    responses, newly_failed, match_failed = results.measure_step(
        step, before, after, lambda key, b, a: _response(step, (4.0, 0.0))
    )
    assert set(responses) == {"left"} and newly_failed == ["right"] and match_failed == []
    responses, newly_failed, match_failed = results.measure_step(
        step, before, {"left": (5.0, 1.0), "right": (2.0, 2.0)}, lambda key, b, a: None
    )
    assert responses == {} and newly_failed == ["left"] and match_failed == ["left"]
    assert results.failed_cameras == {"left", "right"}


def test_a_followup_measures_only_its_camera() -> None:
    results = CalibrationResults()
    step = CalibrationStep(A1, POS, CALIBRATION_STEPS[0].role, duration_ms=900, only_camera="right")
    seen: list[str] = []
    both: dict[str, Measurement] = {"left": (1.0, 1.0), "right": (1.0, 1.0)}
    results.measure_step(step, both, both, lambda key, b, a: seen.append(key) or None)  # type: ignore[func-returns-value]
    assert seen == ["right"]


def test_a_clean_run_builds_both_matrices() -> None:
    results = CalibrationResults()
    _run(results, {"left": _GOOD, "right": _GOOD})
    verdict = results.verdict({})
    assert set(verdict.matrices) == {"left", "right"}
    assert len(verdict.matrices["left"].responses) == 4
    assert verdict.lines[0].startswith("Main: RA-axis dx=+20.0px") and verdict.failure_classes == {}


def test_bounded_and_excluded_cameras_are_explained() -> None:
    results = CalibrationResults()
    _run(results, {"right": _GOOD})
    results.failed_cameras.add("right")
    verdict = results.verdict({"left": "too small"})
    assert verdict.matrices == {}
    assert verdict.lines[0] == "Main: too small."
    assert verdict.lines[1].startswith("Guide: not enough structure to measure a displacement")


def test_parallel_axes_are_degenerate() -> None:
    results = CalibrationResults()
    parallel = dict(_GOOD)
    parallel[(A2, POS)] = (0.0, 0.0)  # field 0270868c: one axis measured nothing
    _run(results, {"left": parallel})
    verdict = results.verdict({})
    assert verdict.failure_classes == {"left": MeasurementFailureClass.CALIBRATION_INVALID}
    assert "too close to parallel" in verdict.lines[0]


def test_a_direction_without_motion_is_no_motion_detected() -> None:
    results = CalibrationResults()
    stuck = dict(_GOOD)
    stuck[(A1, NEG)] = (0.0, 0.0)
    _run(results, {"right": stuck})
    verdict = results.verdict({})
    assert verdict.failure_classes == {"right": MeasurementFailureClass.NO_MOTION_DETECTED}
    assert verdict.lines == [
        "Guide: at least one direction produced no image displacement -- check that the "
        "mount really moves in every direction."
    ]


def test_reset_starts_a_new_attempt() -> None:
    results = CalibrationResults()
    _run(results, {"left": _GOOD})
    results.failed_cameras.add("right")
    results.reset()
    assert results.partial == {"left": {}, "right": {}}
    assert results.characterizations == {"left": {}, "right": {}}
    assert results.failed_cameras == set()
