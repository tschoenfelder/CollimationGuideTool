"""Mount Align capture value types and helpers (S6.6a, moved verbatim out of the panel).

What a both-camera "before"/"after" capture is asked for (`CaptureRequest`, snapshotted on the GUI
thread), what its worker-safe half produces (`CaptureResult`), the threaded job record
(`CaptureJob`, issue #49) and the issue #43 at-rest reference. No Qt.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from astrotool_core.acquisition import (
    DEFAULT_FRESH_FRAME_BUDGET,
    CommandedMovementContext,
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
    MotionAwareFrameResult,
    MotionAwareStatus,
    StableFrameWaiter,
    acquire_verified_frames,
    measurement_reference,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import MountParkStatus, TrackingMode
from astrotool_core.timing import Clock

from collimation_tool.application.mount_align_failures import (
    MeasurementFailureClass,
    capture_failure_class,
)
from collimation_tool.application.mount_align_measurement import Measurement, TargetMode, measure


def frame_acquisition_result(frame: np.ndarray | None) -> FrameAcquisitionResult:
    """Wraps a plain instant frame read as a `FrameAcquisitionResult` --
    `_wait_for_left_frame`/`_wait_for_right_frame`'s own fallback when no
    real `CameraPanel.wait_for_frame_after` is wired (e.g. a test, or any
    caller that doesn't need freshness guarantees), so `_capture_both`'s
    `after_monotonic` branch has exactly one contract to deal with either
    way. `captured_at_monotonic`/`exposure_seconds` are irrelevant here
    (nothing re-checks an already-accepted result's own timing), so both
    are left at 0.0."""
    if frame is None:
        return FrameAcquisitionResult(FrameAcquisitionStatus.CAMERA_UNAVAILABLE)
    return FrameAcquisitionResult(
        FrameAcquisitionStatus.OK,
        DeliveredFrame(pixels=frame, captured_at_monotonic=0.0, exposure_seconds=0.0),
    )


def capture_invalid_result(status: FrameAcquisitionStatus) -> MotionAwareFrameResult:
    """Wraps a bare `FrameAcquisitionStatus` -- the instant/no-wait
    (`verify_stability=False`) path's own only possible failure -- into a
    `MotionAwareFrameResult`, so `self._last_capture_failures` has one
    uniform type regardless of which path produced it and
    `_capture_failure_detail()` doesn't need to know which one did."""
    return MotionAwareFrameResult(
        MotionAwareStatus.CAPTURE_INVALID, diagnostics={"capture_status": status.value}
    )


def stability_evidence_dict(result: MotionAwareFrameResult) -> dict[str, Any]:
    """One capture's worth of `diagnostic_stability_evidence()` --
    `result.diagnostics` already carries `CommandedMovementContext.as_dict()`
    (and, for a `CAPTURE_INVALID` result, `capture_status`); this adds the
    stability check's own outcome on top, when one actually ran (a
    `CAPTURE_INVALID`/`CANCELLED` result reached before any window
    completed leaves `result.stability` `None`)."""
    evidence: dict[str, Any] = {"status": result.status.value, **result.diagnostics}
    if result.stability is not None:
        evidence["stability_status"] = result.stability.status.value
        evidence["max_displacement_px"] = result.stability.max_displacement_px
        evidence["samples_checked"] = result.stability.samples_checked
        evidence["min_score"] = result.stability.min_score
        evidence["tiers"] = list(result.stability.tiers)
    return evidence


#: Issue #43: a verified at-rest reference frame is reusable for the next nudge's BEFORE
#: only while it is this young (drift/lighting) -- older ones are re-captured.
REFERENCE_MAX_AGE_S = 60.0


@dataclass(frozen=True)
class VerifiedReference:
    """Issue #43: a stability-VERIFIED measurement taken while the mount was at rest, and the
    runner's `submit_count` at that moment -- if no sequence was commanded since, it is still
    valid evidence for the next move's BEFORE (the mount has not moved), so a nudge stays
    responsive without ever using an unverified 'latest displayed frame'."""

    taken_at: float
    submit_count: int | None
    mode: str
    measurements: dict[str, Any]


@dataclass(frozen=True)
class CaptureRequest:
    """Everything a both-camera capture needs, read on the GUI thread up front (widget reads
    such as exposure/gain and the fresh-frame budget must not happen on a worker thread)."""

    mode: TargetMode
    label: str | None
    after_monotonic: float | None
    verify_stability: bool
    timeout_s: float
    required_tracking: TrackingMode
    movement_context: CommandedMovementContext | None
    left_state: tuple[float, int] | None
    right_state: tuple[float, int] | None
    #: S6.6b (review C1): False = only VERIFY the mount's tracking state (no start/stop
    #: command) -- the background nudge check, which a newer move may overtake at any time.
    repair_tracking: bool = True


@dataclass
class CaptureResult:
    """The pure outcome of `_capture_blocking` -- applied to the panel on the GUI thread."""

    request: CaptureRequest
    tracking_error: str | None = None
    results: dict[str, MotionAwareFrameResult] | None = None
    instant_failures: dict[str, MotionAwareFrameResult] = field(default_factory=dict)
    left_frame: np.ndarray | None = None
    right_frame: np.ndarray | None = None
    base_reference: float = 0.0
    settle_s: float = 0.0
    raw: dict[str, Measurement | None] = field(default_factory=dict)


@dataclass
class CaptureJob:
    """One capture running on a worker thread (issue #49)."""

    generation: int
    label: str
    request: CaptureRequest
    then: Callable[[dict[str, Measurement] | None], None]
    started_at: float
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: CaptureResult | None = None
    error: BaseException | None = None


def fresh_frame_timeout_s(
    left_state: tuple[float, int] | None, right_state: tuple[float, int] | None
) -> float:
    """How long one freshness wait may take, scaled for the slowest current camera exposure
    (real diagnostic 9ca6daa3: a fixed 2 s budget had no margin left for 2 s exposures).
    Falls back to the budget's floor when no exposure is known. S6.7: the numbers are the
    shared rule's (`DEFAULT_FRESH_FRAME_BUDGET`, its one owner)."""
    exposures_ms = [state[0] for state in (left_state, right_state) if state is not None]
    slowest_s = max(exposures_ms) / 1000.0 if exposures_ms else None
    return DEFAULT_FRESH_FRAME_BUDGET.timeout_s(slowest_s)


def verified_capture_timeout_s(settings: MountAlignmentSettings, fresh_timeout_s: float) -> float:
    """Total deadline for one `verify_stability=True` capture -- it covers the whole
    `stability_sample_count`-frame window, never less than `stability_timeout_s`."""
    return max(settings.stability_timeout_s, fresh_timeout_s * settings.stability_sample_count)


def capture_blocking(
    request: CaptureRequest,
    *,
    tracking_failure: Callable[[TrackingMode, TargetMode], str | None],
    get_left_frame: Callable[[], np.ndarray | None],
    get_right_frame: Callable[[], np.ndarray | None],
    wait_for_left_frame: StableFrameWaiter,
    wait_for_right_frame: StableFrameWaiter,
    settings: MountAlignmentSettings,
    clock: Clock,
    cancelled: Callable[[], bool] | None = None,
) -> CaptureResult:
    """The slow half of a both-camera capture -- tracking verification and the stability-
    verified frame waits (issue #49). Touches no widget and mutates no shared state, so it is
    safe on a worker thread."""
    result = CaptureResult(request)
    result.tracking_error = tracking_failure(request.required_tracking, request.mode)
    if result.tracking_error is not None:
        return result
    if not request.verify_stability:
        result.left_frame = get_left_frame()
        result.right_frame = get_right_frame()
        for key, frame in (("left", result.left_frame), ("right", result.right_frame)):
            if frame is None:
                result.instant_failures[key] = capture_invalid_result(
                    FrameAcquisitionStatus.CAMERA_UNAVAILABLE
                )
    else:
        # The minimum-settle offset only applies when there's an actual prior pulse to settle
        # from -- "prove stability starting right now" for the first step (`after_monotonic`
        # is None) doesn't need it.
        after = request.after_monotonic
        result.base_reference = after if after is not None else clock.monotonic()
        result.settle_s = settings.frame_settle_ms / 1000.0 if after is not None else 0.0
        result.results = acquire_verified_frames(
            {"left": wait_for_left_frame, "right": wait_for_right_frame},
            # S6.7: the shared rule -- exposure must start after the motion ended + settle.
            reference_monotonic=measurement_reference(result.base_reference, result.settle_s),
            timeout_s=request.timeout_s,
            stability_tolerance_px=settings.stability_tolerance_px,
            stability_sample_count=settings.stability_sample_count,
            stability_sample_interval_s=settings.stability_sample_interval_s,
            movement_context=request.movement_context,
            cancelled=cancelled,
            clock=clock,
        )
        result.left_frame = result.results["left"].frame
        result.right_frame = result.results["right"].frame
    result.raw = {
        "left": measure(request.mode, result.left_frame),
        "right": measure(request.mode, result.right_frame),
    }
    return result


@dataclass
class CaptureOutcome:
    """What a finished capture leaves behind (applied by the panel on the GUI thread): the
    per-camera failures and their classes, stability evidence, one timeline entry, the raw
    diagnostic frames, and the measurements -- None when the capture as a whole failed."""

    tracking_error: str | None
    had_movement_context: bool
    failures: dict[str, MotionAwareFrameResult] = field(default_factory=dict)
    stability: dict[str, dict[str, Any]] = field(default_factory=dict)
    failure_classes: dict[str, MeasurementFailureClass] = field(default_factory=dict)
    timeline_entry: dict[str, Any] | None = None
    diagnostic_frames: dict[str, np.ndarray] = field(default_factory=dict)
    diagnostic_camera_state: dict[str, tuple[float, int]] = field(default_factory=dict)
    measurements: dict[str, Measurement] | None = None
    #: True = `measurements` is stability-verified at-rest evidence (issue #43 reference).
    verified: bool = False


def evaluate_capture(
    result: CaptureResult, *, pulse_timing: dict[str, float | None] | None
) -> CaptureOutcome:
    """Classify a finished capture (pure). Issue #30: a camera whose own STABILITY check failed
    (a frame *was* captured, just not verified stable) is simply absent from the measurements
    -- not a reason to null out a camera that succeeded (real diagnostic d14c3a9b). Any OTHER
    reason a camera ends up missing -- no frame at all (CAPTURE_INVALID), or a valid frame this
    mode's own reduction still couldn't use (star mode's "no star detected") -- fails the whole
    capture (`measurements` None). Callers check `if not measurements:` (covers an empty dict)."""
    request = result.request
    outcome = CaptureOutcome(
        tracking_error=result.tracking_error,
        had_movement_context=request.movement_context is not None,
    )
    if result.tracking_error is not None:
        outcome.failure_classes = {
            "left": MeasurementFailureClass.TRACKING_STATE_INVALID,
            "right": MeasurementFailureClass.TRACKING_STATE_INVALID,
        }
        return outcome
    if result.results is None:
        outcome.failures = dict(result.instant_failures)
    else:
        outcome.failures = {key: r for key, r in result.results.items() if not r.ok}
        outcome.stability = {key: stability_evidence_dict(r) for key, r in result.results.items()}
        outcome.timeline_entry = {
            "label": request.label or "",
            "verified": True,
            "reference_monotonic": result.base_reference + result.settle_s,
            "after_monotonic": request.after_monotonic,
            "frame_settle_s": result.settle_s,
            "pulse": dict(pulse_timing) if pulse_timing else None,
            "cameras": {k: dict(v) for k, v in outcome.stability.items()},
        }
    outcome.failure_classes = {
        key: capture_failure_class(failure) for key, failure in outcome.failures.items()
    }
    if request.label is not None:
        for key, frame, state in (
            ("left", result.left_frame, request.left_state),
            ("right", result.right_frame, request.right_state),
        ):
            if frame is not None:
                outcome.diagnostic_frames[f"{request.label}_{key}"] = frame
                if state is not None:
                    outcome.diagnostic_camera_state[f"{request.label}_{key}"] = state
    missing = [key for key, measurement in result.raw.items() if measurement is None]
    if missing and any(
        key not in outcome.failures
        or outcome.failures[key].status is MotionAwareStatus.CAPTURE_INVALID
        for key in missing
    ):
        return outcome
    captured = {k: m for k, m in result.raw.items() if m is not None}
    outcome.measurements = captured
    outcome.verified = request.verify_stability and bool(captured)
    return outcome


def reusable_reference(
    reference: VerifiedReference | None, *, mode: TargetMode, submit_count: int | None, now: float
) -> dict[str, Measurement] | None:
    """Issue #43: the last stability-VERIFIED at-rest measurement, if the mount provably has not
    been commanded since (the runner's `submit_count` is unchanged) and it is young enough;
    else None -> the caller takes a fresh verified capture. Never an unverified frame."""
    if reference is None or reference.submit_count is None or reference.mode != mode:
        return None
    if submit_count != reference.submit_count:
        return None
    if now - reference.taken_at > REFERENCE_MAX_AGE_S:
        return None
    return dict(reference.measurements)


def observed_tracking_failure(
    status: MountParkStatus, required: TrackingMode, mode: TargetMode
) -> str | None:
    """S6.6b (review C1): a READ-ONLY tracking check for the background nudge check -- None when
    a fresh reading shows `required`, else why not. Never sends a command: repairing tracking is
    the enforcer's job on the move paths, and an abandoned check must not race the next move."""
    if not status.fresh:
        return f"the mount was busy, so its tracking could not be read for the {mode} check"
    if not status.available:
        return f"the mount is unavailable, so its tracking could not be read for the {mode} check"
    observed = TrackingMode.ON if status.tracking else TrackingMode.OFF
    if observed is required:
        return None
    return (
        f"mount tracking is {observed.value}, but the {mode} no-motion check needs it "
        f"{required.value}"
    )
