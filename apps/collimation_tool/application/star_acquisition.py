"""FocusedStarAcquisition — issue #15 (Stage 1): track a selected
collimation star through the main camera's ROI, the main camera's full
frame, and (via the guide camera) mount-driven reacquisition when it
leaves the main field entirely.

Composes existing, already-tested primitives rather than reimplementing
any of them: `astrotool_core.target.{detect_sources, select_target,
RoiTracker}` (the exact pattern `guide_tool.application.guide_controller`
already established, applied here to `collimation_tool` for the first
time), `astrotool_core.target.identity_match.resolve_identity` (issue
#15's own new AC 1.6 gap -- neither `RoiTracker` nor `select_target` has
any ambiguity concept), `astrotool_core.registration.alignment.
transform_point_a_to_b` (predicts where the star should appear in the
guide camera from its last known main-camera position), and this
project's own `CollimationRecenterPolicy` (already implements almost all
of AC 1.4/1.5's bounded/iterative/verified/cancellable/divergence-guarded
safety requirements, built for Stage 9's routine recentering but reused
here for Stage 1's guide-driven reacquisition specifically -- the
requirements doc's own text: Stage 9 "complements the guide-camera-
assisted reacquisition from Stage 1", not the reverse).

Deliberately builds no UI and touches no live camera/mount hardware --
`get_guide_frame`/`mount` are caller-supplied (a future Stage 7 UI
session wires them to real hardware); every method here is exercised with
synthetic frames and fake mounts. `select`/`process_main_frame`/
`confirm_returned_to_main` never move anything (mirrors `RoiTracker`'s own
"only ever reports a measured deviation" invariant); only
`attempt_guide_reacquisition` touches the mount, and only once identity is
confidently resolved and the supplied registration's own confidence
clears `min_registration_confidence`.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import numpy as np
from astrotool_core.acquisition import (
    FrameAcquisitionStatus,
    FreshFrameBudget,
    FreshFrameResult,
    FreshFrameSource,
    StableFrameWaiter,
)
from astrotool_core.mount.axis_calibration import CalibrationMatrix
from astrotool_core.mount.operating_mode import OperatingMode
from astrotool_core.mount.port import MountPort
from astrotool_core.registration.alignment import transform_point_a_to_b
from astrotool_core.registration.geometry import polygon_centroid
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import CrossCameraRegistrationResult
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.identity_match import IdentityMatchStatus, resolve_identity
from astrotool_core.target.roi import Roi, compute_roi_bounds
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker, TrackingResult, TrackingState
from astrotool_core.timing import SYSTEM_CLOCK, Clock

from collimation_tool.application.guide_pulse_reacquisition import (
    GuidePulseCalibrationConfig,
    ReacquisitionMotion,
    calibrate_guide_pulses,
    choose_reacquisition_motion,
)
from collimation_tool.application.mount_commands import rearm_after_stop
from collimation_tool.application.mount_motion import MotionClaim, MountMotionExecutor
from collimation_tool.application.recenter_policy import (
    CollimationRecenterPolicy,
    MountCorrectionResult,
    RecenterConfig,
)

#: S6.0b (re-review): the largest image shift one reacquisition move may produce PER AXIS,
#: bounded by two things. (1) 0.35 x the guide tracker's search radius
#: (`guide_max_position_error_px`): the measurement after each correction must re-find the star
#: inside that radius, and both axes move before it -- a combined jump of up to sqrt(2) x the
#: per-axis step, i.e. <= ~0.5 x radius with a perfect calibration and <= ~0.74 x radius when
#: the real response is 1.5x the calibration (e.g. calibrated at another declination). 0.5 x
#: radius per axis lost the star in that case (coordinator decision after review).
#: (2) A quarter of the guide frame's smaller dimension: the same 25%-of-frame size Mount Align
#: (#46) calibrates with -- a move size the rig has demonstrably measured -- so a wrong-signed
#: move cannot push the star off the frame in one step. Never below the mount's smallest move
#: (the policy raises a cap to that floor). Without a frame the policy's fixed `max_pulse_ms`
#: cap (~60" at 8x) applies. OnStepAdapter's 10 degree maximum is far above either bound (a
#: refusal beyond it would be reported, not hidden).
_STEP_TRACKER_RADIUS_FRACTION = 0.35
_STEP_FRAME_FRACTION = 0.25

#: S6.7: every reacquisition measurement after a move uses a guide frame whose exposure started
#: after the move ended plus the policy's settle (`fresh_frame_after_motion`, the rule's one
#: owner). Until a frame shows the guide camera's real exposure the budget assumes 2.5 s (two
#: exposures + margin = 6 s, the ~2 s guide exposures seen in field diagnostics); a frame
#: showing a longer exposure extends it, so long exposures are waited for, not timed out.
_GUIDE_FRESH_FRAME_BUDGET = FreshFrameBudget(assumed_exposure_s=2.5)

#: S6.7: no fresh guide frame arrived in the exposure-scaled time after a move (`detail` says
#: what was seen) -- the run stops instead of measuring a stale frame.
FRESH_FRAME_TIMEOUT = "fresh_frame_timeout"
#: S6.7: the guide camera stopped streaming during the run.
GUIDE_CAMERA_UNAVAILABLE = "guide_camera_unavailable"
#: S6.7: no fresh-frame source (`wait_guide_frame_after`) was given -- nothing is moved, since
#: no measurement after a move could be trusted.
NO_FRESH_FRAME_SOURCE = "no_fresh_frame_source"
_NO_FRESH_FRAME_SOURCE_DETAIL = (
    "no fresh-frame source for the guide camera: a measurement after a mount move could not be "
    "proven to come from a frame exposed after the move -- nothing was moved"
)
#: The measure()-failure reasons a frame-acquisition failure stands behind.
_LOST_STAR_REASONS = frozenset({"star_lost", "target_not_found_guide"})

#: Issue #15's own machine-readable failure-reason vocabulary, verbatim.
_CORRECTION_REASON_TO_FAILURE_REASON = {
    "star_lost": "target_not_found_guide",
    "diverging": "reacquisition_diverging",
    "pulse_rejected": "mount_correction_rejected",
    "max_pulses": "reacquisition_timeout",
    "cancelled": "cancelled",
}

#: S6.0d: nothing was sent -- astronomical reacquisition is not possible right now
#: (`AcquisitionResult.detail` says why, e.g. "needs OnStepAdapter >= 0.5.0").
ASTRONOMICAL_REACQUISITION_UNAVAILABLE = "astronomical_reacquisition_unavailable"
#: S6.8a: another workflow is moving the mount (one in-flight mount motion app-wide); nothing
#: was sent, the busy reason is in `detail`.
MOUNT_BUSY = "mount_busy"
#: The claim owner name another workflow's busy refusal shows.
REACQUISITION_OWNER = "guide-assisted reacquisition"
#: S6.0d review (A2, #44 intent): the operating mode left Astronomical during a guide-pulse
#: reacquisition -- no further guide pulse was sent.
OPERATING_MODE_CHANGED = "operating_mode_changed"
_MODE_CHANGED_DETAIL = (
    "the operating mode switched away from Astronomical -- no further guide pulses were sent"
)
#: S6.0d: a sent guide pulse failed and the mount is NOT tracking any more (OnStepAdapter's
#: emergency stop, or a Stop) -- the user must re-enable tracking.
MOUNT_TRACKING_STOPPED = "mount_tracking_stopped"


class AcquisitionStatus(Enum):
    TRACKING = "tracking"
    SEARCHING_MAIN = "searching_main"
    SEARCHING_GUIDE = "searching_guide"
    LOST = "lost"
    AMBIGUOUS = "ambiguous"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AcquisitionResult:
    status: AcquisitionStatus
    roi: Roi | None
    target_position: tuple[float, float] | None
    failure_reason: str | None = None
    #: S6.0b: human-readable detail behind a failure -- e.g. the mount adapter's own refusal
    #: reason behind `mount_correction_rejected` (never dropped). None when there is none.
    detail: str | None = None


def _require_position(result: TrackingResult) -> tuple[float, float]:
    """`TrackingResult.x`/`.y` are typed `float | None` (a genuinely
    unset position is possible in principle), but every call site here
    only ever reads them right after `acquire()` or a `LOCKED`/
    `REACQUIRED` `update()` -- both of which always echo back a real
    position by `RoiTracker`'s own contract."""
    assert result.x is not None and result.y is not None
    return (result.x, result.y)


def _with_warnings(message: str, warnings: tuple[str, ...]) -> str | None:
    """S6.0d: OnStepAdapter's guide-pulse warnings (e.g. meridian_flip_required) appended to a
    detail text -- never dropped. None when there is nothing to say."""
    parts = [message] if message else []
    if warnings:
        parts.append(f"OnStepAdapter warnings: {', '.join(warnings)}")
    return "; ".join(parts) or None


def _fresh_frame_failure(
    reason: str, frame: FreshFrameResult | None, warnings: tuple[str, ...] = ()
) -> AcquisitionResult | None:
    """S6.7: a lost star behind which the fresh-frame wait failed is that failure, said
    explicitly (timeout / camera gone / cancelled) -- not a 'star lost'. None otherwise."""
    if reason not in _LOST_STAR_REASONS or frame is None or frame.ok:
        return None
    if frame.status is FrameAcquisitionStatus.CANCELLED:
        return AcquisitionResult(AcquisitionStatus.CANCELLED, None, None, "cancelled")
    failure = (
        GUIDE_CAMERA_UNAVAILABLE
        if frame.status is FrameAcquisitionStatus.CAMERA_UNAVAILABLE
        else FRESH_FRAME_TIMEOUT
    )
    detail = f"guide frame after the move: {frame.describe()}"
    return AcquisitionResult(
        AcquisitionStatus.LOST, None, None, failure, _with_warnings(detail, warnings)
    )


def _failed_correction(
    correction: MountCorrectionResult,
    warnings: tuple[str, ...],
    frame: FreshFrameResult | None = None,
) -> AcquisitionResult:
    if not correction.tracking_off:
        frame_failure = _fresh_frame_failure(correction.reason, frame, warnings)
        if frame_failure is not None:
            return frame_failure
    failure_reason = _CORRECTION_REASON_TO_FAILURE_REASON.get(correction.reason, correction.reason)
    if correction.tracking_off:  # S6.0d: the mount stopped tracking -- say so first
        failure_reason = MOUNT_TRACKING_STOPPED
    status = (
        AcquisitionStatus.CANCELLED if correction.reason == "cancelled" else AcquisitionStatus.LOST
    )
    return AcquisitionResult(
        status, None, None, failure_reason, _with_warnings(correction.message, warnings)
    )


class FocusedStarAcquisition:
    def __init__(
        self,
        *,
        roi_size: tuple[int, int],
        main_tracker: RoiTracker | None = None,
        max_full_frame_search_attempts: int = 5,
        max_position_error_px: float = 60.0,
        guide_max_position_error_px: float = 80.0,
        min_registration_confidence: float = 0.5,
    ) -> None:
        self._roi_size = roi_size
        # Identity has already been resolved externally (resolve_identity,
        # AC 1.6) by the time anything is ever handed to this tracker, so
        # its own tolerance is set to match max_position_error_px rather
        # than RoiTracker's own tight 8.0px default -- that default would
        # otherwise reject a real, already-confirmed reacquisition
        # (AC 1.3: the whole point is following the star well outside a
        # small ROI-sized move).
        self._tracker = main_tracker or RoiTracker(
            lock_tolerance_px=max_position_error_px, search_radius_px=max_position_error_px
        )
        self._max_full_frame_search_attempts = max_full_frame_search_attempts
        self._max_position_error_px = max_position_error_px
        self._guide_max_position_error_px = guide_max_position_error_px
        self._min_registration_confidence = min_registration_confidence
        self._full_frame_search_count = 0
        self._last_position: tuple[float, float] | None = None
        self._last_peak: float | None = None
        self._roi: Roi | None = None
        #: S6.0d: the guide-pulse calibration measured in this session (None = not yet).
        self._guide_pulse_matrix: CalibrationMatrix | None = None

    def select(self, main_frame: np.ndarray) -> AcquisitionResult:
        """AC 1.1: detect + select_target + tracker.acquire(); returns
        the initial ROI, centered on the selected star."""
        detection = detect_sources(main_frame)
        target = select_target(detection)
        if target is None:
            return AcquisitionResult(AcquisitionStatus.LOST, None, None, "target_not_found_main")

        acquired = self._tracker.acquire(target.x, target.y)
        self._last_position = _require_position(acquired)
        self._last_peak = target.peak
        self._full_frame_search_count = 0
        self._roi = compute_roi_bounds(main_frame.shape, self._last_position, self._roi_size)
        return AcquisitionResult(
            AcquisitionStatus.TRACKING, self._roi, self._last_position, None
        )

    def process_main_frame(self, main_frame: np.ndarray) -> AcquisitionResult:
        """AC 1.2/1.3: resolve identity against the predicted (= last
        known) position across the WHOLE main frame -- a star that moved
        outside its old ROI but stays in-frame is reacquired here, the
        ROI moves to it, and this never reports `LOST` for that case
        (only for "not found anywhere in main, retries exhausted").
        Ambiguous candidates short-circuit before the tracker is ever
        touched (AC 1.6)."""
        if self._last_position is None:
            raise RuntimeError("process_main_frame() called before select()")

        detection = detect_sources(main_frame)
        identity = resolve_identity(
            detection.sources,
            predicted_position=self._last_position,
            expected_peak=self._last_peak,
            max_position_error_px=self._max_position_error_px,
        )

        if identity.status is IdentityMatchStatus.AMBIGUOUS:
            return AcquisitionResult(
                AcquisitionStatus.AMBIGUOUS, self._roi, None, "target_ambiguous"
            )

        if identity.status is IdentityMatchStatus.NOT_FOUND:
            self._tracker.update(())
            self._full_frame_search_count += 1
            if self._full_frame_search_count >= self._max_full_frame_search_attempts:
                return AcquisitionResult(
                    AcquisitionStatus.LOST, None, None, "target_not_found_main"
                )
            return AcquisitionResult(AcquisitionStatus.SEARCHING_MAIN, self._roi, None, None)

        assert identity.source is not None  # MATCHED guarantees a source
        tracked = self._tracker.update([identity.source])
        self._full_frame_search_count = 0
        self._last_position = _require_position(tracked)
        self._last_peak = identity.source.peak
        self._roi = compute_roi_bounds(main_frame.shape, self._last_position, self._roi_size)
        return AcquisitionResult(AcquisitionStatus.TRACKING, self._roi, self._last_position, None)

    def attempt_guide_reacquisition(
        self,
        get_guide_frame: Callable[[], np.ndarray | None],
        *,
        mount: MountPort,
        guide_calibration: CalibrationMatrix | None,
        registration: CrossCameraRegistrationResult,
        prior_main: OpticalPrior,
        cancel_check: Callable[[], bool] | None = None,
        clock: Clock | None = None,
        center_rate_x: float | None = None,
        operating_mode: OperatingMode | None = None,
        tracking_state: Callable[[], bool | None] | None = None,
        wait_guide_frame_after: StableFrameWaiter | None = None,
        guide_pulse_calibration: GuidePulseCalibrationConfig | None = None,
        current_operating_mode: Callable[[], OperatingMode] | None = None,
        exposure_hint: Callable[[], float | None] | None = None,
        motion_executor: MountMotionExecutor | None = None,
    ) -> AcquisitionResult:
        """AC 1.4: only meant to be called once `process_main_frame` has
        reported `target_not_found_main` (i.e. exhausted its own bounded
        retries) -- predicts the guide-space position from the last
        known main-camera position via `transform_point_a_to_b`, resolves
        identity in a first guide frame (ambiguous/not-found short-
        circuit before any mount command), and -- only if
        `registration.confidence` clears `min_registration_confidence`
        (issue: "Automatic... recentering shall require sufficient
        calibration confidence") -- drives `CollimationRecenterPolicy`
        toward `polygon_centroid(registration.polygon_a_in_b)` (the
        center of Main's own FOV as seen in Guide: comfortably inside
        Main's field once reached, not merely at its edge). Synchronous
        and run-to-completion, matching `CollimationRecenterPolicy.
        center()`'s own contract -- one final `AcquisitionResult`, not
        interim progress.

        S6.0b: `clock` is the policy's settle clock (default real); `center_rate_x` is the
        unit `guide_calibration` was stored in on a mount without timed pulses (pass the loaded
        `MountAlignmentSettings.calibration_center_rate_x`; None = that setting's default). On
        such a mount the policy moves angularly (never `pulse_axis`); a refusal's adapter
        reason is returned in `detail`. Once the gates pass and the mount is about to move,
        the adapter's latched Stop is re-armed (`rearm_after_stop`), like any new command.

        S6.0d: the motion is chosen up front from `operating_mode` (the global mode's value;
        None = terrestrial behaviour) and, in Astronomical mode, `tracking_state()` (a fresh
        reading, None = unknown) -- see `guide_pulse_reacquisition`. A refusal is
        `astronomical_reacquisition_unavailable` with the reason in `detail`, nothing sent. On
        the guide-pulse path `guide_calibration` (Mount Align's angular matrix) is not used: the
        guide pulses are calibrated here once per session. OnStepAdapter warnings go to `detail`
        on success; a failed pulse after which the mount no longer tracks is
        `mount_tracking_stopped`. `current_operating_mode()` (the mode's owner, re-read) is
        checked between guide pulses: leaving Astronomical ends the run as
        `operating_mode_changed`, nothing further sent.

        S6.7 (both paths): every measurement during the run uses a frame from
        `wait_guide_frame_after` whose exposure STARTED after the preceding move ended plus the
        policy's settle (`RecenterConfig.settle_ms`), waited for under an exposure-scaled
        deadline (`fresh_frame_after_motion`); `get_guide_frame` (the latest displayed frame) is
        only read once, for the identity check before anything moves. Without
        `wait_guide_frame_after` nothing moves (`no_fresh_frame_source`); a wait that ends
        without a fresh frame ends the run as `fresh_frame_timeout` (or
        `guide_camera_unavailable`), the reason in `detail` -- never a stale measurement.

        S6.8a (R3/R4): with `motion_executor` (the app-wide one) the whole motion part --
        guide-pulse calibration and the closed loop -- runs under ONE claim: while another
        workflow moves the mount nothing is sent (`mount_busy`, the busy reason in `detail`),
        and the executor's `abort()` (fine-collimation Cancel) cancels the run: the GOTO in
        flight is stopped, a settle or fresh-frame wait ends at once, no further step is sent.
        The claim re-arms the adapter's Stop latch; without an executor the latch is re-armed
        directly (`rearm_after_stop`)."""
        decision = choose_reacquisition_motion(operating_mode, tracking_state, mount)
        if decision.motion is None:
            return AcquisitionResult(
                AcquisitionStatus.LOST, None, None, ASTRONOMICAL_REACQUISITION_UNAVAILABLE,
                decision.refusal,
            )
        guide_pulses = decision.motion is ReacquisitionMotion.GUIDE_PULSE
        if not guide_pulses and guide_calibration is None:
            return AcquisitionResult(AcquisitionStatus.LOST, None, None, "no_guide_calibration")
        if self._last_position is None:
            raise RuntimeError("attempt_guide_reacquisition() called before select()")

        predicted_guide_position = transform_point_a_to_b(
            self._last_position, prior_main, registration
        )

        first_frame = get_guide_frame()
        if first_frame is None:
            return AcquisitionResult(AcquisitionStatus.LOST, None, None, "target_not_found_guide")

        detection = detect_sources(first_frame)
        identity = resolve_identity(
            detection.sources,
            predicted_position=predicted_guide_position,
            expected_peak=self._last_peak,
            max_position_error_px=self._guide_max_position_error_px,
        )
        if identity.status is IdentityMatchStatus.AMBIGUOUS:
            return AcquisitionResult(AcquisitionStatus.AMBIGUOUS, None, None, "target_ambiguous")
        if identity.status is IdentityMatchStatus.NOT_FOUND:
            return AcquisitionResult(
                AcquisitionStatus.LOST, None, None, "target_not_found_guide"
            )
        assert identity.source is not None

        confidence = registration.confidence
        if confidence is None or confidence < self._min_registration_confidence:
            return AcquisitionResult(
                AcquisitionStatus.SEARCHING_GUIDE,
                None, (identity.source.x, identity.source.y),
                "insufficient_calibration_confidence",
            )

        assert registration.polygon_a_in_b is not None  # guaranteed alongside confidence/.ok
        target_center = polygon_centroid(registration.polygon_a_in_b)
        # Both tolerances generous and equal, not RoiTracker's own tight
        # 8.0px default -- CollimationRecenterPolicy.center() treats ANY
        # non-LOCKED/REACQUIRED measurement as an immediate "star_lost"
        # abort (no tolerance for a transient miss), so a single
        # mount pulse moving the star further than `lock_tolerance_px`
        # would otherwise abort a genuinely-converging correction on its
        # very first post-pulse measurement.
        guide_tracker = RoiTracker(
            lock_tolerance_px=self._guide_max_position_error_px,
            search_radius_px=self._guide_max_position_error_px,
        )
        guide_tracker.acquire(identity.source.x, identity.source.y)

        if wait_guide_frame_after is None:
            return AcquisitionResult(
                AcquisitionStatus.LOST, None, None, NO_FRESH_FRAME_SOURCE,
                _NO_FRESH_FRAME_SOURCE_DETAIL,
            )
        # S6.8a: one in-flight mount motion app-wide -- claimed once every gate has passed.
        claim: MotionClaim | None = None
        if motion_executor is not None:
            claimed = motion_executor.claim(REACQUISITION_OWNER, mount)
            if isinstance(claimed, str):
                return AcquisitionResult(AcquisitionStatus.LOST, None, None, MOUNT_BUSY, claimed)
            claim = claimed
        else:
            # S6.0b: a new operator command -- an earlier Stop (e.g. Mount Align's) must not
            # silently refuse it; a Stop pressed DURING this reacquisition still latches.
            rearm_after_stop(mount)
        try:
            return self._move_toward_target(
                mount, claim, guide_tracker, first_frame, target_center,
                wait_guide_frame_after=wait_guide_frame_after,
                guide_pulses=guide_pulses,
                guide_calibration=guide_calibration,
                cancel_check=cancel_check,
                clock=clock,
                center_rate_x=center_rate_x,
                guide_pulse_calibration=guide_pulse_calibration,
                current_operating_mode=current_operating_mode,
                exposure_hint=exposure_hint,
            )
        finally:
            if motion_executor is not None and claim is not None:
                motion_executor.release(claim)

    def _move_toward_target(
        self,
        mount: MountPort,
        claim: MotionClaim | None,
        guide_tracker: RoiTracker,
        first_frame: np.ndarray,
        target_center: tuple[float, float],
        *,
        wait_guide_frame_after: StableFrameWaiter,
        guide_pulses: bool,
        guide_calibration: CalibrationMatrix | None,
        cancel_check: Callable[[], bool] | None,
        clock: Clock | None,
        center_rate_x: float | None,
        guide_pulse_calibration: GuidePulseCalibrationConfig | None,
        current_operating_mode: Callable[[], OperatingMode] | None,
        exposure_hint: Callable[[], float | None] | None,
    ) -> AcquisitionResult:
        """The motion part of `attempt_guide_reacquisition` (S6.8a: under `claim`, whose
        cancel counts as the caller's cancel everywhere)."""
        caller_cancel = cancel_check

        def motion_cancelled() -> bool:
            if claim is not None and claim.cancelled:
                return True
            return caller_cancel is not None and caller_cancel()

        cancel_check = motion_cancelled
        # S6.7: the policy's settle becomes part of the fresh-frame rule (the exposure must
        # start after the move ended + settle) instead of a blind sleep before a latest frame.
        # S6.6b: `exposure_hint` = the guide camera's exposure (s) as the GUI thread last
        # snapshotted it, so even the FIRST wait is scaled for a long exposure.
        frames = FreshFrameSource(
            wait_guide_frame_after,
            settle_s=RecenterConfig().settle_ms / 1000.0,
            budget=_GUIDE_FRESH_FRAME_BUDGET,
            exposure_hint=exposure_hint,
            clock=clock or SYSTEM_CLOCK,
        )

        def measure() -> TrackingResult:
            # Called right after a blocking move returned (or before the first one): the
            # motion has ended now.
            fresh = frames.after_motion(cancelled=cancel_check)
            if not fresh.ok or fresh.frame is None:
                return TrackingResult(
                    state=TrackingState.LOST, x=None, y=None, matched_source=None
                )
            return guide_tracker.update(detect_sources(fresh.frame.pixels).sources)

        max_step_px = min(
            _STEP_TRACKER_RADIUS_FRACTION * self._guide_max_position_error_px,
            _STEP_FRAME_FRACTION * min(first_frame.shape[:2]),
        )
        if guide_pulses:
            return self._recenter_with_guide_pulses(
                mount,
                measure,
                target_center,
                cancel_check=cancel_check,
                current_operating_mode=current_operating_mode,
                clock=clock,
                max_step_px=max_step_px,
                calibration_config=guide_pulse_calibration,
                frames=frames,
                claim=claim,
            )
        assert guide_calibration is not None  # checked above for the angular path
        policy = CollimationRecenterPolicy(
            mount,
            guide_calibration,
            RecenterConfig(settle_ms=0),  # S6.7: the settle is in the fresh-frame rule
            clock=clock,
            center_rate_x=center_rate_x,
            max_step_px=max_step_px,
            motion_claim=claim,
        )
        correction = policy.center(measure, reference=target_center, cancel_check=cancel_check)
        if correction.success:
            return AcquisitionResult(AcquisitionStatus.SEARCHING_GUIDE, None, target_center, None)
        return _failed_correction(correction, (), frames.last_result)

    def _recenter_with_guide_pulses(
        self,
        mount: MountPort,
        measure: Callable[[], TrackingResult],
        target_center: tuple[float, float],
        *,
        cancel_check: Callable[[], bool] | None,
        current_operating_mode: Callable[[], OperatingMode] | None,
        clock: Clock | None,
        max_step_px: float,
        calibration_config: GuidePulseCalibrationConfig | None,
        frames: FreshFrameSource,
        claim: MotionClaim | None = None,
    ) -> AcquisitionResult:
        """S6.0d: calibrate the guide pulses once per session, then the closed loop. Between
        pulses the operating mode is re-read (review A2): leaving Astronomical ends the run."""
        mode_changed: list[bool] = []

        def guide_cancel() -> bool:
            if (
                current_operating_mode is not None
                and current_operating_mode() is not OperatingMode.ASTRONOMICAL
            ):
                mode_changed.append(True)
                return True
            return cancel_check is not None and cancel_check()

        result = self._guide_pulse_session(
            mount,
            measure,
            target_center,
            cancel_check=guide_cancel,
            clock=clock,
            max_step_px=max_step_px,
            calibration_config=calibration_config,
            frames=frames,
            claim=claim,
        )
        if mode_changed:
            return AcquisitionResult(
                AcquisitionStatus.LOST, None, None, OPERATING_MODE_CHANGED, _MODE_CHANGED_DETAIL
            )
        return result

    def _guide_pulse_session(
        self,
        mount: MountPort,
        measure: Callable[[], TrackingResult],
        target_center: tuple[float, float],
        *,
        cancel_check: Callable[[], bool],
        clock: Clock | None,
        max_step_px: float,
        calibration_config: GuidePulseCalibrationConfig | None,
        frames: FreshFrameSource,
        claim: MotionClaim | None = None,
    ) -> AcquisitionResult:
        calibrated = self._guide_pulse_matrix
        calibration_warnings: tuple[str, ...] = ()
        if calibrated is None:
            outcome = calibrate_guide_pulses(
                mount,
                measure,
                config=calibration_config,
                cancel_check=cancel_check,
                # Review A1: the calibration excursion stays within one reacquisition step.
                max_displacement_px=max_step_px,
            )
            if outcome.matrix is None:
                assert outcome.failure_reason is not None
                frame_failure = _fresh_frame_failure(
                    outcome.failure_reason, frames.last_result, outcome.warnings
                )
                if frame_failure is not None:
                    return frame_failure
                status = (
                    AcquisitionStatus.CANCELLED
                    if outcome.failure_reason == "cancelled"
                    else AcquisitionStatus.LOST
                )
                return AcquisitionResult(
                    status, None, None, outcome.failure_reason,
                    _with_warnings(outcome.message, outcome.warnings),
                )
            calibrated = self._guide_pulse_matrix = outcome.matrix
            calibration_warnings = outcome.warnings
        policy = CollimationRecenterPolicy(
            mount,
            calibrated,
            RecenterConfig(settle_ms=0),  # S6.7: the settle is in the fresh-frame rule
            clock=clock,
            max_step_px=max_step_px,
            guide_pulses=True,
            motion_claim=claim,
        )
        correction = policy.center(measure, reference=target_center, cancel_check=cancel_check)
        warnings = tuple(sorted(set(calibration_warnings) | set(correction.warnings)))
        if correction.success:
            return AcquisitionResult(
                AcquisitionStatus.SEARCHING_GUIDE, None, target_center, None,
                _with_warnings("", warnings),
            )
        return _failed_correction(correction, warnings, frames.last_result)

    def confirm_returned_to_main(self, main_frame: np.ndarray) -> AcquisitionResult:
        """AC 1.5: after `attempt_guide_reacquisition` reports success,
        re-run main-frame detection and resolve identity (position
        prediction is necessarily coarse here -- the star has just
        re-entered the field, so the frame's own center stands in for
        "predicted position", generously toleranced to cover the whole
        frame; brightness consistency still guards against grabbing an
        unrelated bright star). On a confirmed match, re-`acquire()`s the
        main tracker fresh and transitions back to `TRACKING` with a new
        ROI -- no new manual selection needed."""
        height, width = main_frame.shape
        predicted_position = (width / 2.0, height / 2.0)
        max_error_px = math.hypot(width, height) / 2.0

        detection = detect_sources(main_frame)
        identity = resolve_identity(
            detection.sources,
            predicted_position=predicted_position,
            expected_peak=self._last_peak,
            max_position_error_px=max_error_px,
        )
        if identity.status is IdentityMatchStatus.AMBIGUOUS:
            return AcquisitionResult(AcquisitionStatus.AMBIGUOUS, None, None, "target_ambiguous")
        if identity.status is IdentityMatchStatus.NOT_FOUND:
            return AcquisitionResult(AcquisitionStatus.LOST, None, None, "reacquisition_timeout")

        assert identity.source is not None
        acquired = self._tracker.acquire(identity.source.x, identity.source.y)
        self._last_position = _require_position(acquired)
        self._last_peak = identity.source.peak
        self._full_frame_search_count = 0
        self._roi = compute_roi_bounds(main_frame.shape, self._last_position, self._roi_size)
        return AcquisitionResult(AcquisitionStatus.TRACKING, self._roi, self._last_position, None)
