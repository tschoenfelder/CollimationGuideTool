"""S6.6a: Mount Align capture, measurement, failure classification and wording -- moved out of
`MountTestMovePanel` unchanged. Tested directly (no panel, no MainWindow, no Qt)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from astrotool_core.acquisition import (
    DEFAULT_FRESH_FRAME_BUDGET,
    CommandedMovementContext,
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
    MotionAwareFrameResult,
    MotionAwareStatus,
    StabilityCheckResult,
    StabilityStatus,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, AxisResponse, MountAxis, TrackingMode
from astrotool_core.timing.fake_clock import FakeClock
from collimation_tool.application.mount_align_capture import (
    REFERENCE_MAX_AGE_S,
    CaptureRequest,
    CaptureResult,
    VerifiedReference,
    capture_blocking,
    capture_invalid_result,
    evaluate_capture,
    frame_acquisition_result,
    fresh_frame_timeout_s,
    reusable_reference,
    stability_evidence_dict,
    verified_capture_timeout_s,
)
from collimation_tool.application.mount_align_failures import (
    MeasurementFailureClass,
    bounded_failure_class,
    capture_failure_class,
    is_no_motion,
)
from collimation_tool.application.mount_align_measurement import build_response, measure
from collimation_tool.application.mount_align_messages import (
    EXPOSURE_OVERLAPPED_NO_MOVE_MESSAGE,
    MOTION_STATUS_MESSAGES,
    STABILITY_INDETERMINATE_MESSAGE,
    STATUS_MESSAGES,
    bounded_text,
    degenerate_calibration_message,
    describe_capture_failures,
    format_response,
    missing_label,
    no_motion_text,
    response_dict,
)

pytestmark = pytest.mark.unit


def _star_frame(x: int, y: int, shape: tuple[int, int] = (64, 64)) -> np.ndarray:
    frame = np.full(shape, 100.0)
    frame[y - 1 : y + 2, x - 1 : x + 2] = 5000.0
    return frame


def _request(**overrides: Any) -> CaptureRequest:  # noqa: ANN401
    values: dict[str, Any] = {
        "mode": "star",
        "label": "nudge_before",
        "after_monotonic": None,
        "verify_stability": True,
        "timeout_s": 5.0,
        "required_tracking": TrackingMode.OFF,
        "movement_context": None,
        "left_state": (100.0, 10),
        "right_state": None,
    }
    values.update(overrides)
    return CaptureRequest(**values)


def _response(dx: float, dy: float, axis: MountAxis = MountAxis.AXIS1) -> AxisResponse:
    return AxisResponse(axis, AxisDirection.POSITIVE, 1000, dx, dy, 0.0)


# ---- failure classification ---------------------------------------------------------------
def test_capture_invalid_is_a_capture_failure_everything_else_is_not_stable() -> None:
    invalid = capture_invalid_result(FrameAcquisitionStatus.TIMEOUT)
    assert capture_failure_class(invalid) is MeasurementFailureClass.CAPTURE_INVALID
    for status in (MotionAwareStatus.IMAGE_NOT_STABLE, MotionAwareStatus.SETTLE_TIMEOUT):
        result = MotionAwareFrameResult(status)
        assert capture_failure_class(result) is MeasurementFailureClass.IMAGE_NOT_STABLE


def test_bounded_probe_class_follows_the_sizing_reason() -> None:
    assert bounded_failure_class("overshoot_at_minimum") is MeasurementFailureClass.MOVE_TOO_LARGE
    assert bounded_failure_class("too_small") is MeasurementFailureClass.MOVE_EXCEEDS_ENVELOPE
    assert bounded_failure_class(None) is MeasurementFailureClass.MOVE_EXCEEDS_ENVELOPE


def test_no_motion_floor_is_one_pixel_or_a_fraction_of_the_frame_width() -> None:
    assert is_no_motion(_response(0.9, 0.0), (1.0, 2.0))
    assert not is_no_motion(_response(1.0, 0.0), (1.0, 2.0))
    wide = np.zeros((10, 2000))  # 0.25 % of 2000 px = 5 px
    assert is_no_motion(_response(4.9, 0.0), wide)
    assert not is_no_motion(_response(5.0, 0.0), wide)


# ---- wording ----------------------------------------------------------------------------
def test_capture_failure_note_names_each_camera_and_cause() -> None:
    assert describe_capture_failures({}, had_movement_context=True) == ""
    failures = {
        "left": capture_invalid_result(FrameAcquisitionStatus.TIMEOUT),
        "right": MotionAwareFrameResult(MotionAwareStatus.SETTLE_TIMEOUT),
    }
    assert describe_capture_failures(failures, had_movement_context=True) == (
        f" (Main: {STATUS_MESSAGES[FrameAcquisitionStatus.TIMEOUT]}; "
        f"Guide: {MOTION_STATUS_MESSAGES[MotionAwareStatus.SETTLE_TIMEOUT]})"
    )


def test_overlap_without_a_commanded_move_gets_the_no_move_wording() -> None:
    overlap = {"left": capture_invalid_result(FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION)}
    assert EXPOSURE_OVERLAPPED_NO_MOVE_MESSAGE in describe_capture_failures(
        overlap, had_movement_context=False
    )
    assert STATUS_MESSAGES[
        FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION
    ] in describe_capture_failures(overlap, had_movement_context=True)


def test_indeterminate_stability_is_not_blamed_on_wind() -> None:
    indeterminate = MotionAwareFrameResult(
        MotionAwareStatus.IMAGE_NOT_STABLE,
        stability=StabilityCheckResult(StabilityStatus.INDETERMINATE, None, 2),
    )
    unstable = MotionAwareFrameResult(
        MotionAwareStatus.IMAGE_NOT_STABLE,
        stability=StabilityCheckResult(StabilityStatus.UNSTABLE, 4.0, 2),
    )
    assert STABILITY_INDETERMINATE_MESSAGE in describe_capture_failures(
        {"right": indeterminate}, had_movement_context=True
    )
    assert "wind/vibration" in describe_capture_failures(
        {"right": unstable}, had_movement_context=True
    )


def test_labels_and_exclusion_texts() -> None:
    assert missing_label("star") == "no star detected"
    assert missing_label("terrestrial") == "no frame available"
    assert "commanded 400 ms move at preset 6" in no_motion_text(400, "6")
    assert "(3 s at preset 6)" in bounded_text(
        MeasurementFailureClass.MOVE_EXCEEDS_ENVELOPE, max_pulse_ms=3000, rate_preset="6"
    )
    assert "overshoots" in bounded_text(
        MeasurementFailureClass.MOVE_TOO_LARGE, max_pulse_ms=3000, rate_preset="6"
    )


def test_response_formatting_and_degenerate_explanation() -> None:
    response = _response(3.0, -4.0)
    assert format_response(response) == "dx=+3.0px dy=-4.0px (5.0px @ 307°)"
    assert response_dict(response)["magnitude_px"] == pytest.approx(5.0)
    zero = _response(0.0, 0.0, MountAxis.AXIS2)
    other_moved = {MountAxis.AXIS2: _response(10.0, 0.0, MountAxis.AXIS2)}
    text = degenerate_calibration_message("left", response, zero, other_moved)
    assert text.startswith("Main: RA-axis and Dec-axis too close to parallel -- ")
    assert "Guide confirms real motion" in text
    assert "on either camera" in degenerate_calibration_message("left", response, zero, {})


# ---- measurement ------------------------------------------------------------------------
def test_star_mode_measures_the_brightest_source_and_terrestrial_keeps_the_frame() -> None:
    frame = _star_frame(20, 30)
    centroid = measure("star", frame)
    assert centroid == pytest.approx((20.0, 30.0), abs=0.5)
    assert measure("terrestrial", frame) is frame
    assert measure("star", None) is None
    assert measure("star", np.full((32, 32), 100.0)) is None


def test_star_response_is_the_centroid_shift() -> None:
    response = build_response(
        "star", MountAxis.AXIS1, AxisDirection.POSITIVE, 500, (10.0, 10.0), (13.0, 14.0)
    )
    assert response is not None
    assert (response.dx_px, response.dy_px, response.duration_ms) == (3.0, 4.0, 500)


def test_terrestrial_response_is_the_correlated_shift() -> None:
    rng = np.random.default_rng(3)
    before = rng.normal(1000.0, 200.0, (96, 96))
    after = np.roll(before, (2, 5), axis=(0, 1))
    response = build_response(
        "terrestrial", MountAxis.AXIS2, AxisDirection.NEGATIVE, 100, before, after
    )
    assert response is not None
    assert (round(response.dx_px), round(response.dy_px)) == (5, 2)
    flat = np.full((96, 96), 500.0)
    assert (
        build_response("terrestrial", MountAxis.AXIS1, AxisDirection.POSITIVE, 100, flat, flat)
        is None
    )


# ---- capture ----------------------------------------------------------------------------
def test_timeouts_follow_the_shared_fresh_frame_budget() -> None:
    assert fresh_frame_timeout_s(None, None) == DEFAULT_FRESH_FRAME_BUDGET.timeout_s(None)
    assert fresh_frame_timeout_s((500.0, 1), (2000.0, 1)) == DEFAULT_FRESH_FRAME_BUDGET.timeout_s(
        2.0
    )
    settings = MountAlignmentSettings(stability_timeout_s=1.0, stability_sample_count=3)
    assert verified_capture_timeout_s(settings, 4.0) == 12.0
    floor = MountAlignmentSettings(stability_timeout_s=30.0, stability_sample_count=3)
    assert verified_capture_timeout_s(floor, 4.0) == 30.0


def test_frame_wrappers() -> None:
    frame = np.zeros((4, 4))
    assert frame_acquisition_result(None).status is FrameAcquisitionStatus.CAMERA_UNAVAILABLE
    wrapped = frame_acquisition_result(frame)
    assert wrapped.ok and wrapped.frame is not None and wrapped.frame.pixels is frame
    evidence = stability_evidence_dict(
        MotionAwareFrameResult(
            MotionAwareStatus.OK,
            stability=StabilityCheckResult(StabilityStatus.STABLE, 0.2, 3, 0.9, ("ncc",)),
            diagnostics={"movement_type": "nudge"},
        )
    )
    assert evidence == {
        "status": "ok",
        "movement_type": "nudge",
        "stability_status": "stable",
        "max_displacement_px": 0.2,
        "samples_checked": 3,
        "min_score": 0.9,
        "tiers": ["ncc"],
    }


def test_a_tracking_failure_captures_nothing() -> None:
    calls: list[str] = []
    result = capture_blocking(
        _request(),
        tracking_failure=lambda required, mode: f"tracking not {required.value} for {mode}",
        get_left_frame=lambda: calls.append("left") or None,  # type: ignore[func-returns-value]
        get_right_frame=lambda: calls.append("right") or None,  # type: ignore[func-returns-value]
        wait_for_left_frame=lambda _r, _t: FrameAcquisitionResult(FrameAcquisitionStatus.TIMEOUT),
        wait_for_right_frame=lambda _r, _t: FrameAcquisitionResult(FrameAcquisitionStatus.TIMEOUT),
        settings=MountAlignmentSettings(),
        clock=FakeClock(),
    )
    assert result.tracking_error is not None and calls == []
    outcome = evaluate_capture(result, pulse_timing=None)
    assert outcome.measurements is None
    assert outcome.failure_classes == {
        "left": MeasurementFailureClass.TRACKING_STATE_INVALID,
        "right": MeasurementFailureClass.TRACKING_STATE_INVALID,
    }
    assert outcome.timeline_entry is None and outcome.diagnostic_frames == {}


def _verified_capture(frames: dict[str, np.ndarray | None], **request: Any) -> CaptureResult:  # noqa: ANN401
    clock = FakeClock(start=100.0)

    def waiter(key: str) -> Any:  # noqa: ANN401
        def wait(reference: float, _timeout: float) -> FrameAcquisitionResult:
            frame = frames[key]
            if frame is None:
                return FrameAcquisitionResult(FrameAcquisitionStatus.CAMERA_UNAVAILABLE)
            clock.advance(0.1)
            return FrameAcquisitionResult(
                FrameAcquisitionStatus.OK,
                DeliveredFrame(
                    frame, captured_at_monotonic=clock.monotonic(), exposure_seconds=0.01
                ),
            )

        return wait

    return capture_blocking(
        _request(**request),
        tracking_failure=lambda _required, _mode: None,
        get_left_frame=lambda: None,
        get_right_frame=lambda: None,
        wait_for_left_frame=waiter("left"),
        wait_for_right_frame=waiter("right"),
        settings=MountAlignmentSettings(
            frame_settle_ms=0, stability_sample_interval_s=0.0, stability_sample_count=2
        ),
        clock=clock,
    )


def test_a_verified_capture_measures_both_cameras_and_keeps_its_evidence() -> None:
    result = _verified_capture({"left": _star_frame(20, 20), "right": _star_frame(40, 10)})
    outcome = evaluate_capture(result, pulse_timing={"completed_at": 1.0})
    assert outcome.measurements is not None and set(outcome.measurements) == {"left", "right"}
    assert outcome.verified and outcome.failures == {} and outcome.failure_classes == {}
    assert set(outcome.diagnostic_frames) == {"nudge_before_left", "nudge_before_right"}
    assert outcome.diagnostic_camera_state == {"nudge_before_left": (100.0, 10)}
    assert outcome.timeline_entry is not None
    assert outcome.timeline_entry["label"] == "nudge_before"
    assert outcome.timeline_entry["pulse"] == {"completed_at": 1.0}
    assert set(outcome.stability) == {"left", "right"}


def test_a_camera_without_a_frame_fails_the_whole_capture() -> None:
    result = _verified_capture({"left": _star_frame(20, 20), "right": None})
    outcome = evaluate_capture(result, pulse_timing=None)
    assert outcome.measurements is None and not outcome.verified
    assert outcome.failure_classes == {"right": MeasurementFailureClass.CAPTURE_INVALID}
    assert set(outcome.diagnostic_frames) == {"nudge_before_left"}  # evidence is still kept


def test_no_star_in_a_valid_frame_fails_the_whole_capture() -> None:
    ok = MotionAwareFrameResult(MotionAwareStatus.OK, frame=np.full((8, 8), 100.0))
    result = CaptureResult(
        _request(), results={"left": ok, "right": ok}, raw={"left": (20.0, 20.0), "right": None}
    )
    outcome = evaluate_capture(result, pulse_timing=None)
    assert outcome.measurements is None and outcome.failures == {}


def test_an_unstable_camera_is_only_left_out() -> None:
    request = _request()
    result = CaptureResult(
        request,
        results={
            "left": MotionAwareFrameResult(MotionAwareStatus.OK, frame=_star_frame(20, 20)),
            "right": MotionAwareFrameResult(MotionAwareStatus.IMAGE_NOT_STABLE),
        },
        raw={"left": (20.0, 20.0), "right": None},
    )
    outcome = evaluate_capture(result, pulse_timing=None)
    assert outcome.measurements == {"left": (20.0, 20.0)}
    assert outcome.failure_classes == {"right": MeasurementFailureClass.IMAGE_NOT_STABLE}


def test_the_instant_path_reads_the_latest_frames() -> None:
    result = capture_blocking(
        _request(verify_stability=False, mode="terrestrial", label=None),
        tracking_failure=lambda _required, _mode: None,
        get_left_frame=lambda: np.ones((4, 4)),
        get_right_frame=lambda: None,
        wait_for_left_frame=lambda _r, _t: FrameAcquisitionResult(FrameAcquisitionStatus.TIMEOUT),
        wait_for_right_frame=lambda _r, _t: FrameAcquisitionResult(FrameAcquisitionStatus.TIMEOUT),
        settings=MountAlignmentSettings(),
        clock=FakeClock(),
    )
    assert result.results is None and set(result.instant_failures) == {"right"}
    outcome = evaluate_capture(result, pulse_timing=None)
    assert outcome.measurements is None and outcome.timeline_entry is None
    assert outcome.had_movement_context is False


def test_movement_context_is_reported() -> None:
    context = CommandedMovementContext(movement_type="nudge", duration_ms=10)
    result = CaptureResult(_request(movement_context=context), tracking_error="x")
    assert evaluate_capture(result, pulse_timing=None).had_movement_context is True


def test_the_at_rest_reference_is_reused_only_while_valid() -> None:
    reference = VerifiedReference(
        taken_at=10.0, submit_count=3, mode="star", measurements={"left": (1.0, 2.0)}
    )
    assert reusable_reference(reference, mode="star", submit_count=3, now=10.0) == {
        "left": (1.0, 2.0)
    }
    assert reusable_reference(None, mode="star", submit_count=3, now=10.0) is None
    assert reusable_reference(reference, mode="terrestrial", submit_count=3, now=10.0) is None
    assert reusable_reference(reference, mode="star", submit_count=4, now=10.0) is None
    edge = 10.0 + REFERENCE_MAX_AGE_S
    assert reusable_reference(reference, mode="star", submit_count=3, now=edge) is not None
    assert reusable_reference(reference, mode="star", submit_count=3, now=edge + 0.001) is None
    no_count = VerifiedReference(10.0, None, "star", {})
    assert reusable_reference(no_count, mode="star", submit_count=None, now=10.0) is None
