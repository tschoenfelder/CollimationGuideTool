"""AutofocusController — issue #33's shared workflow: computes the hard
safety envelope and drives BoundedFocusSearcher (`autofocus_search.py`)
with whichever mode-specific analyzer (star or terrestrial) was selected,
while enforcing frame-freshness and a stable exposure/gain for the whole
run.

Same constructor-injected-callables DI shape `MountTestMovePanel` already
established for its own camera access (`get_left_frame`,
`wait_for_left_frame` == `CameraPanel.wait_for_frame_after`,
`set_left_auto_exposure_paused` == `CameraPanel.set_auto_exposure_paused`)
-- this controller takes exactly that shape of callables for whichever
camera panel/focuser pairing it's attached to, so it never depends on
`CameraPanel`/Qt directly and stays independently testable.

Frame-freshness: `BoundedFocusSearcher` itself already waits for the
focuser to report movement complete before ever calling this controller's
own `measure` closure (see `autofocus_search.BoundedFocusSearcher.
_wait_for_move_settled`) -- so by the time `measure()` runs here, the
physical move is already known complete, and passing `time.monotonic()`
(captured right then) as `wait_for_frame`'s own `reference_monotonic` is
guaranteed to reject any frame whose exposure started before that.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import FrameAcquisitionResult
from astrotool_core.focus.port import FocuserPort
from astrotool_core.focus.search_bounds import DEFAULT_ENVELOPE_STEPS
from astrotool_core.focus.star_focus_metric import StarTargetTracker, measure_star_focus
from astrotool_core.focus.terrestrial_focus_metric import measure_terrestrial_focus

from collimation_tool.application.autofocus_search import (
    AutofocusStatus,
    BoundedFocusSearcher,
    FocusCurvePoint,
    FocusSample,
    InvalidSample,
)

GetFrame = Callable[[], "np.ndarray | None"]
WaitForFrame = Callable[[float, float], FrameAcquisitionResult]
SetAutoExposurePaused = Callable[[bool], None]


class AutofocusMode(Enum):
    STAR = "star"
    TERRESTRIAL = "terrestrial"
    #: Issue #33 enhancement: exactly ONE artificial star, star-specific
    #: metric, same target through the sweep -- a first-class mode, not a
    #: fallback to terrestrial whole-frame sharpness.
    ARTIFICIAL_STAR = "artificial_star"


@dataclass(frozen=True)
class ExposureControl:
    """How the controller may lower exposure when the artificial star
    saturates at focus (issue #33): `get`/`set` read/apply the camera's
    `(exposure_ms, gain)` (the setter must clamp to the camera's own
    limits and be safe to call from the runner thread), `settle_s` lets
    the new setting take effect before the next frame is requested."""

    get: Callable[[], tuple[float, int]]
    set: Callable[[float, int], None]
    settle_s: float = 0.5
    factor: float = 0.5


@dataclass(frozen=True)
class AutofocusResult:
    status: AutofocusStatus
    mode: AutofocusMode
    start_position: int
    best_position: int | None
    search_min: int | None
    search_max: int | None
    samples: tuple[FocusCurvePoint, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    final_value: float | None = None
    #: Issue #35: which camera/optical-train/focuser this run actually
    #: used. Defaulted to "unknown" (not required) so existing callers
    #: are unaffected; this app has no independent per-train focuser
    #: yet, so camera_label/optical_train/focuser_label are today always
    #: the same single value, passed in by whichever panel owns this
    #: controller -- not three independently-tracked identities.
    camera_label: str = "unknown"
    optical_train: str = "unknown"
    focuser_label: str = "unknown"
    #: Issue #33 (artificial star): why no result (`saturated`,
    #: `multiple_candidates`, `donut_like`, `saturated_at_min_exposure`, ...),
    #: the target the sweep followed, the metric at the start position, and
    #: the `(exposure_ms, gain)` of each attempt.
    failure_reason: str | None = None
    tracked_target: tuple[float, float] | None = None
    start_value: float | None = None
    exposure_attempts: tuple[tuple[float, int], ...] = ()


class AutofocusController:
    def __init__(
        self,
        focuser: FocuserPort,
        *,
        get_frame: GetFrame,
        wait_for_frame: WaitForFrame,
        set_auto_exposure_paused: SetAutoExposurePaused,
        frame_timeout_s: float = 5.0,
        terrestrial_sample_count: int = 3,
        coarse_step: int = 250,
        fine_step: int = 25,
        max_coarse_steps: int = 20,
        max_consecutive_no_improve: int = 2,
        improvement_fraction: float = 0.05,
        final_approach_direction: int = 1,
        envelope_steps: int = DEFAULT_ENVELOPE_STEPS,
        star_edge_margin_px: int = 16,
        terrestrial_tile_size_px: int = 64,
        camera_label: str = "unknown",
        focuser_label: str = "unknown",
        exposure_control: ExposureControl | None = None,
        max_exposure_attempts: int = 3,
        tracker_max_shift_px: float = 40.0,
    ) -> None:
        self._exposure_control = exposure_control
        self._max_exposure_attempts = max_exposure_attempts
        self._tracker_max_shift_px = tracker_max_shift_px
        self._focuser = focuser
        self._get_frame = get_frame
        self._wait_for_frame = wait_for_frame
        self._set_auto_exposure_paused = set_auto_exposure_paused
        self._camera_label = camera_label
        self._focuser_label = focuser_label
        self._frame_timeout_s = frame_timeout_s
        self._terrestrial_sample_count = max(1, terrestrial_sample_count)
        self._star_edge_margin_px = star_edge_margin_px
        self._terrestrial_tile_size_px = terrestrial_tile_size_px
        self._coarse_step = coarse_step
        self._fine_step = fine_step
        self._max_coarse_steps = max_coarse_steps
        self._max_consecutive_no_improve = max_consecutive_no_improve
        self._improvement_fraction = improvement_fraction
        self._final_approach_direction = final_approach_direction
        self._envelope_steps = envelope_steps

    def run(
        self, mode: AutofocusMode, cancel_check: Callable[[], bool] | None = None
    ) -> AutofocusResult:
        self._set_auto_exposure_paused(True)
        artificial = mode is AutofocusMode.ARTIFICIAL_STAR
        control = self._exposure_control if artificial else None
        original = control.get() if control is not None else None
        attempts: list[tuple[float, int]] = [original] if original is not None else []
        tracker = StarTargetTracker(max_shift_px=self._tracker_max_shift_px)
        override: tuple[AutofocusStatus, str] | None = None
        try:
            for attempt in range(self._max_exposure_attempts if control is not None else 1):
                tracker = StarTargetTracker(max_shift_px=self._tracker_max_shift_px)
                searcher = BoundedFocusSearcher(
                    self._focuser, higher_is_better=(mode is AutofocusMode.TERRESTRIAL),
                    coarse_step=self._coarse_step, fine_step=self._fine_step,
                    max_coarse_steps=self._max_coarse_steps,
                    max_consecutive_no_improve=self._max_consecutive_no_improve,
                    improvement_fraction=self._improvement_fraction,
                    final_approach_direction=self._final_approach_direction,
                    envelope_steps=self._envelope_steps,
                    allow_invalid_samples=artificial, require_improvement=artificial,
                )
                measure = self._build_measurer(mode, tracker)
                search_result = searcher.search(measure, cancel_check=cancel_check)
                saturated = any(
                    point.reason == "saturated" for point in search_result.samples
                )
                if (
                    control is None
                    or not saturated
                    or search_result.status is AutofocusStatus.CANCELLED
                    or attempt + 1 >= self._max_exposure_attempts
                ):
                    break
                # Saturated at (or on the way to) focus: lower the exposure,
                # return to the original P0 (so the +-envelope is unchanged)
                # and search again with the star's peak back below full scale.
                exposure_ms, gain = control.get()
                control.set(exposure_ms * control.factor, gain)
                time.sleep(control.settle_s)
                new_ms, new_gain = control.get()
                if new_ms >= exposure_ms * 0.999:
                    override = (AutofocusStatus.NO_USABLE_EVIDENCE, "saturated_at_min_exposure")
                    break
                attempts.append((new_ms, new_gain))
                self._return_to(search_result.start_position)
        finally:
            if control is not None and original is not None and control.get() != original:
                control.set(*original)
            self._set_auto_exposure_paused(False)

        valid = [point for point in search_result.samples if point.valid]
        confidence = valid[-1].confidence if valid else 0.0
        status = search_result.status
        failure_reason = search_result.failure_reason
        if override is not None:
            status, failure_reason = override
        elif (
            artificial
            and status is AutofocusStatus.NO_USABLE_EVIDENCE
            and failure_reason is None
        ):
            failure_reason = "no_star"
        return AutofocusResult(
            status=status,
            mode=mode,
            start_position=search_result.start_position,
            best_position=search_result.best_position,
            search_min=search_result.search_min,
            search_max=search_result.search_max,
            samples=search_result.samples,
            confidence=confidence,
            final_value=search_result.final_value,
            camera_label=self._camera_label,
            optical_train=self._camera_label,
            focuser_label=self._focuser_label,
            failure_reason=failure_reason,
            tracked_target=tracker.target if artificial else None,
            start_value=search_result.start_value,
            exposure_attempts=tuple(attempts),
        )

    def _return_to(self, position: int) -> None:
        """Best-effort return to the original start position between
        exposure attempts (same bounds as the search itself: it is P0)."""
        self._focuser.move_absolute(position)
        deadline = time.monotonic() + 10.0
        while self._focuser.is_moving() and time.monotonic() < deadline:
            time.sleep(0.05)

    def _build_measurer(
        self, mode: AutofocusMode, tracker: StarTargetTracker
    ) -> Callable[[], FocusSample | InvalidSample | None]:
        if mode is AutofocusMode.STAR:
            return self._measure_star
        if mode is AutofocusMode.ARTIFICIAL_STAR:
            return lambda: self._measure_artificial_star(tracker)
        return self._measure_terrestrial

    def _measure_artificial_star(
        self, tracker: StarTargetTracker
    ) -> FocusSample | InvalidSample | None:
        frame = self._acquire_fresh_frame()
        if frame is None:
            return None  # frame acquisition failed -- still aborts the run
        measurement = (
            tracker.acquire(frame) if tracker.target is None else tracker.measure(frame)
        )
        if measurement.fwhm_px is None:
            return InvalidSample(measurement.reason or "no_star")
        # A single star is a usable but reduced-confidence measurement
        # (issue #33: one suitable star => appropriately reduced confidence).
        return FocusSample(value=measurement.fwhm_px, confidence=1.0 / 3.0)

    def _acquire_fresh_frame(self) -> np.ndarray | None:
        reference_monotonic = time.monotonic()
        acquisition = self._wait_for_frame(reference_monotonic, self._frame_timeout_s)
        if not acquisition.ok or acquisition.frame is None:
            return None
        return acquisition.frame.pixels

    def _measure_star(self) -> FocusSample | None:
        frame = self._acquire_fresh_frame()
        if frame is None:
            return None
        measurement = measure_star_focus(frame, edge_margin_px=self._star_edge_margin_px)
        if measurement.fwhm_px is None:
            return None
        return FocusSample(value=measurement.fwhm_px, confidence=measurement.confidence)

    def _measure_terrestrial(self) -> FocusSample | None:
        first = self._acquire_fresh_frame()
        if first is None:
            return None
        frames: list[np.ndarray] = [first]
        for _ in range(self._terrestrial_sample_count - 1):
            extra = self._get_frame()
            if extra is not None:
                frames.append(extra)
        measurement = measure_terrestrial_focus(frames, tile_size_px=self._terrestrial_tile_size_px)
        if measurement.sharpness is None:
            return None
        return FocusSample(value=measurement.sharpness, confidence=measurement.confidence)
