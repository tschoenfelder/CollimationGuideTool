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
from astrotool_core.mount.axis_calibration import CalibrationMatrix
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

from collimation_tool.application.recenter_policy import CollimationRecenterPolicy

#: Issue #15's own machine-readable failure-reason vocabulary, verbatim.
_CORRECTION_REASON_TO_FAILURE_REASON = {
    "star_lost": "target_not_found_guide",
    "diverging": "reacquisition_diverging",
    "pulse_rejected": "mount_correction_rejected",
    "max_pulses": "reacquisition_timeout",
    "cancelled": "cancelled",
}


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


def _require_position(result: TrackingResult) -> tuple[float, float]:
    """`TrackingResult.x`/`.y` are typed `float | None` (a genuinely
    unset position is possible in principle), but every call site here
    only ever reads them right after `acquire()` or a `LOCKED`/
    `REACQUIRED` `update()` -- both of which always echo back a real
    position by `RoiTracker`'s own contract."""
    assert result.x is not None and result.y is not None
    return (result.x, result.y)


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
        guide_calibration: CalibrationMatrix,
        registration: CrossCameraRegistrationResult,
        prior_main: OpticalPrior,
        cancel_check: Callable[[], bool] | None = None,
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
        interim progress."""
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

        def measure() -> TrackingResult:
            frame = get_guide_frame()
            if frame is None:
                return TrackingResult(
                    state=TrackingState.LOST, x=None, y=None, matched_source=None
                )
            return guide_tracker.update(detect_sources(frame).sources)

        policy = CollimationRecenterPolicy(mount, guide_calibration)
        correction = policy.center(measure, reference=target_center, cancel_check=cancel_check)

        if correction.success:
            return AcquisitionResult(AcquisitionStatus.SEARCHING_GUIDE, None, target_center, None)

        failure_reason = _CORRECTION_REASON_TO_FAILURE_REASON.get(
            correction.reason, correction.reason
        )
        status = (
            AcquisitionStatus.CANCELLED
            if correction.reason == "cancelled"
            else AcquisitionStatus.LOST
        )
        return AcquisitionResult(status, None, None, failure_reason)

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
