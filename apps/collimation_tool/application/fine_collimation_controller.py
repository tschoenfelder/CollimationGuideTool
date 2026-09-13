"""FineCollimationController — issue #21 (Stage 7): the first live
orchestration of Stages 1→6 together. Nothing else in the app ever
wires a real `FocusedStarAcquisition` (#15) through registration (#16),
stacking (#17), the radial profile (#18), the optical reference model
(#19), and the symmetry measurement (#20) into one result — each stage
shipped as a pure library function/class with no caller. This is that
caller.

Guide-camera-assisted reacquisition (`FocusedStarAcquisition.
attempt_guide_reacquisition`) is explicitly NOT wired into this loop —
see the issue #21 plan's own "Scope decision, flagged explicitly". A
star that leaves the Main frame and isn't reacquired within
`FocusedStarAcquisition`'s own bounded full-frame retries is reported
as a clean, explained failure (`"star_lost"`), matching AC 7.2's own
"insufficient... shown why not actionable" framing rather than
attempting mount-driven recovery.

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
    AcquisitionStatus,
    FocusedStarAcquisition,
)

GetFrame = Callable[[], "np.ndarray | None"]

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


@dataclass(frozen=True)
class FineCollimationOutcome:
    """`result` is `None` exactly when `status` is `"failed"` --
    `reason` explains which failure (see the `_*_REASON` constants
    above), continuing #15/#18-#20's own `None`-when-invalid +
    `reason: str` idiom."""

    status: str  # "success" | "failed"
    result: FineCollimationResult | None
    reason: str | None


class FineCollimationController:
    def __init__(
        self,
        acquisition: FocusedStarAcquisition,
        *,
        get_frame: GetFrame,
        optical_config: OpticalConfig,
        sample_count: int = 8,
        max_attempts: int | None = None,
    ) -> None:
        self._acquisition = acquisition
        self._get_frame = get_frame
        self._optical_config = optical_config
        self._sample_count = sample_count
        self._max_attempts = (
            max_attempts
            if max_attempts is not None
            else sample_count * _DEFAULT_MAX_ATTEMPTS_FACTOR
        )

    def run(self, cancel_check: Callable[[], bool] | None = None) -> FineCollimationOutcome:
        collected: list[np.ndarray] = []
        attempts = 0
        selected = False

        while len(collected) < self._sample_count:
            if cancel_check is not None and cancel_check():
                return FineCollimationOutcome(
                    status="failed", result=None, reason=_CANCELLED_REASON
                )
            if attempts >= self._max_attempts:
                return FineCollimationOutcome(
                    status="failed", result=None, reason=_ACQUISITION_FAILURE_REASON
                )

            frame = self._get_frame()
            attempts += 1
            if frame is None:
                if attempts >= self._max_attempts:
                    return FineCollimationOutcome(
                        status="failed", result=None, reason=_NO_FRAME_REASON
                    )
                continue

            if not selected:
                result = self._acquisition.select(frame)
                selected = True
            else:
                result = self._acquisition.process_main_frame(frame)

            if result.status is AcquisitionStatus.TRACKING:
                assert result.roi is not None
                collected.append(crop_to_roi(frame, result.roi))
            elif result.status in (AcquisitionStatus.LOST, AcquisitionStatus.CANCELLED):
                reason = _STAR_LOST_REASON if result.status is AcquisitionStatus.LOST else (
                    _CANCELLED_REASON
                )
                return FineCollimationOutcome(status="failed", result=None, reason=reason)
            # SEARCHING_MAIN / AMBIGUOUS: not yet usable, not yet fatal --
            # keep going, bounded by attempts/max_attempts above.

        return self._build_result(collected)

    def _build_result(self, roi_frames: list[np.ndarray]) -> FineCollimationOutcome:
        registration = register_frames(roi_frames)
        buffer = RollingFrameBuffer(capacity=len(roi_frames))
        for outcome in registration.outcomes:
            if outcome.usable and outcome.pixels is not None and outcome.star is not None:
                buffer.add(outcome.pixels, outcome.star)

        stack_result = buffer.produce_stack()
        if stack_result.stacked is None:
            return FineCollimationOutcome(
                status="failed", result=None, reason=_NO_USABLE_FRAMES_REASON
            )

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
            ),
            reason=None,
        )
