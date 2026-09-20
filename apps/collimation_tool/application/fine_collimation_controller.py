"""FineCollimationController — issue #21 (Stage 7): the first live
orchestration of Stages 1→6 together. Nothing else in the app ever
wires a real `FocusedStarAcquisition` (#15) through registration (#16),
stacking (#17), the radial profile (#18), the optical reference model
(#19), and the symmetry measurement (#20) into one result — each stage
shipped as a pure library function/class with no caller. This is that
caller.

Guide-assisted reacquisition (issue #39): an optional `guide_reacquirer`
(normally a closure over `FocusedStarAcquisition.attempt_guide_reacquisition`,
built where the guide frame/mount/calibration/registration live) recovers a
star lost from Main mid-run; `confirm_returned_to_main` re-detects with
`resolve_identity`, so an ambiguous return fails as `target_ambiguous`
instead of silently switching target. Without a reacquirer -- or when the
first selection itself fails -- a lost star is a clean `star_lost` (#21).

`target_mode` (issue #39) is carried onto the result: an ARTIFICIAL_STAR is
at a finite distance, so the UI must not present a symmetric result as a
final fine-collimation verdict.

Same constructor-injected-callables DI shape every other controller
this session used (`AutofocusController`) — `get_frame`/
`optical_config` are caller-supplied, no direct camera/Qt dependency,
independently testable with synthetic frames.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from astrotool_core.diffraction.optical_reference_model import (
    DiffractionReferenceResult,
    OpticalConfig,
    compute_diffraction_reference,
)
from astrotool_core.diffraction.radial_profile import RadialProfileResult, compute_radial_profile
from astrotool_core.diffraction.symmetry_measurement import (
    SymmetryMeasurementResult,
    compute_symmetry_measurement,
)
from astrotool_core.target.frame_registration import register_frames
from astrotool_core.target.roi import crop_to_roi
from astrotool_core.target.stacking import RollingFrameBuffer, StackResult

from collimation_tool.application.star_acquisition import (
    AcquisitionResult,
    AcquisitionStatus,
    FocusedStarAcquisition,
)
from collimation_tool.domain.target_mode import CollimationTargetMode

GetFrame = Callable[[], "np.ndarray | None"]
#: Issue #39: recovers a star lost from Main via the guide camera --
#: `(acquisition, cancel_check) -> AcquisitionResult`, normally a thin
#: closure over `FocusedStarAcquisition.attempt_guide_reacquisition`
#: built where the guide frame/mount/calibration/registration live.
GuideReacquirer = Callable[
    [FocusedStarAcquisition, "Callable[[], bool] | None"], AcquisitionResult
]

#: `FocusedStarAcquisition` reports its own bounded exhaustion as
#: `LOST`/`target_not_found_main` (or a similarly self-bounded status)
#: -- this caps how many total `process_main_frame` CALLS (not just
#: usable ones) this controller will make before giving up on its own,
#: an outer safety net rather than the primary bound.
_DEFAULT_MAX_ATTEMPTS_FACTOR = 4

#: Machine-readable failure reasons, mirroring #15/#18-#20's own
#: `reason: str` idiom -- explains *why* no result, never just "failed".
_ACQUISITION_FAILURE_REASON = "acquisition_failed"
_STAR_LOST_REASON = "star_lost"
_NO_USABLE_FRAMES_REASON = "no_usable_frames"
_CANCELLED_REASON = "cancelled"
_NO_FRAME_REASON = "no_frame_available"


@dataclass(frozen=True)
class FineCollimationResult:
    stack_result: StackResult
    profile_result: RadialProfileResult
    reference_result: DiffractionReferenceResult
    symmetry_result: SymmetryMeasurementResult
    target_mode: CollimationTargetMode = CollimationTargetMode.NATURAL_STAR


@dataclass(frozen=True)
class FineCollimationOutcome:
    """`result` is `None` exactly when `status` is `"failed"` --
    `reason` explains which failure (see the `_*_REASON` constants
    above), continuing #15/#18-#20's own `None`-when-invalid +
    `reason: str` idiom."""

    status: str  # "success" | "failed"
    result: FineCollimationResult | None
    reason: str | None
    #: Issue #39: what guide-assisted reacquisition did during this run
    #: (e.g. "reacquired_via_guide"), retained for diagnostics.
    reacquisition_log: tuple[str, ...] = ()


class FineCollimationController:
    def __init__(
        self,
        acquisition: FocusedStarAcquisition,
        *,
        get_frame: GetFrame,
        optical_config: OpticalConfig,
        sample_count: int = 8,
        max_attempts: int | None = None,
        target_mode: CollimationTargetMode = CollimationTargetMode.NATURAL_STAR,
        guide_reacquirer: GuideReacquirer | None = None,
        max_reacquisitions: int = 1,
    ) -> None:
        self._target_mode = target_mode
        self._guide_reacquirer = guide_reacquirer
        self._max_reacquisitions = max_reacquisitions
        self._acquisition = acquisition
        self._get_frame = get_frame
        self._optical_config = optical_config
        self._sample_count = sample_count
        self._max_attempts = (
            max_attempts
            if max_attempts is not None
            else sample_count * _DEFAULT_MAX_ATTEMPTS_FACTOR
        )

    def _fail(self, reason: str, log: list[str]) -> FineCollimationOutcome:
        return FineCollimationOutcome(
            status="failed", result=None, reason=reason, reacquisition_log=tuple(log)
        )

    def run(self, cancel_check: Callable[[], bool] | None = None) -> FineCollimationOutcome:
        collected: list[np.ndarray] = []
        log: list[str] = []
        attempts = 0
        reacquisitions = 0
        selected = False

        while len(collected) < self._sample_count:
            if cancel_check is not None and cancel_check():
                return self._fail(_CANCELLED_REASON, log)
            if attempts >= self._max_attempts:
                return self._fail(_ACQUISITION_FAILURE_REASON, log)

            frame = self._get_frame()
            attempts += 1
            if frame is None:
                if attempts >= self._max_attempts:
                    return self._fail(_NO_FRAME_REASON, log)
                continue

            first_call = not selected
            if first_call:
                result = self._acquisition.select(frame)
                selected = True
            else:
                result = self._acquisition.process_main_frame(frame)

            if result.status is AcquisitionStatus.TRACKING:
                assert result.roi is not None
                collected.append(crop_to_roi(frame, result.roi))
            elif result.status is AcquisitionStatus.CANCELLED:
                return self._fail(_CANCELLED_REASON, log)
            elif result.status is AcquisitionStatus.LOST:
                can_reacquire = (
                    not first_call
                    and self._guide_reacquirer is not None
                    and reacquisitions < self._max_reacquisitions
                )
                if not can_reacquire:
                    return self._fail(_STAR_LOST_REASON, log)
                assert self._guide_reacquirer is not None
                reacquisitions += 1
                log.append("guide_reacquisition_attempted")
                recovered = self._recover_via_guide(cancel_check, log)
                if isinstance(recovered, str):
                    return self._fail(recovered, log)
                collected.append(recovered)
            # SEARCHING_MAIN / AMBIGUOUS: not yet usable, not yet fatal --
            # keep going, bounded by attempts/max_attempts above.

        return self._build_result(collected, log)

    def _recover_via_guide(
        self, cancel_check: Callable[[], bool] | None, log: list[str]
    ) -> np.ndarray | str:
        """Guide-assisted recovery (issue #39): returns the recovered ROI
        frame, or a failure-reason string. Identity is preserved -- the
        confirm step re-detects with `resolve_identity`, so an ambiguous
        return reports `target_ambiguous` instead of switching target."""
        assert self._guide_reacquirer is not None
        guide_result = self._guide_reacquirer(self._acquisition, cancel_check)
        if guide_result.status is AcquisitionStatus.CANCELLED:
            return _CANCELLED_REASON
        if guide_result.status is not AcquisitionStatus.SEARCHING_GUIDE:
            return guide_result.failure_reason or _STAR_LOST_REASON
        frame = self._get_frame()
        if frame is None:
            return _NO_FRAME_REASON
        confirmed = self._acquisition.confirm_returned_to_main(frame)
        if confirmed.status is not AcquisitionStatus.TRACKING or confirmed.roi is None:
            return confirmed.failure_reason or _STAR_LOST_REASON
        log.append("reacquired_via_guide")
        return crop_to_roi(frame, confirmed.roi)

    def _build_result(
        self, roi_frames: list[np.ndarray], log: list[str]
    ) -> FineCollimationOutcome:
        registration = register_frames(roi_frames)
        buffer = RollingFrameBuffer(capacity=len(roi_frames))
        for outcome in registration.outcomes:
            if outcome.usable and outcome.pixels is not None and outcome.star is not None:
                buffer.add(outcome.pixels, outcome.star)

        stack_result = buffer.produce_stack()
        if stack_result.stacked is None:
            return self._fail(_NO_USABLE_FRAMES_REASON, log)

        profile_result = compute_radial_profile(stack_result.stacked)
        reference_result = compute_diffraction_reference(self._optical_config)
        symmetry_result = compute_symmetry_measurement(
            stack_result.stacked, profile_result, reference_result
        )

        return FineCollimationOutcome(
            status="success",
            result=FineCollimationResult(
                stack_result=stack_result,
                profile_result=profile_result,
                reference_result=reference_result,
                symmetry_result=symmetry_result,
                target_mode=self._target_mode,
            ),
            reason=None,
            reacquisition_log=tuple(log),
        )
