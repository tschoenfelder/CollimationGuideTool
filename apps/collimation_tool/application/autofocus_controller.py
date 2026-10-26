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
physical move is already known complete, and passing the clock's
`monotonic()` (captured right then) as `wait_for_frame`'s own `reference_monotonic` is
guaranteed to reject any frame whose exposure started before that.

S6.7 (T05): that wait is the shared fresh-frame-after-motion rule
(`astrotool_core.acquisition.fresh_frame_after_motion`) -- an exposure-scaled
deadline instead of the fixed 5 s that made every exposure above ~2 s on a
streaming camera fail as "frame acquisition failed". Until a frame shows the
camera's real exposure the budget assumes 2 s (= the old 5 s); a delivered
frame with a longer exposure extends it, and later waits of the run are scaled
from the exposure last seen (e.g. lowered by the artificial-star exposure
control). No widget is read for it (the run is on a worker thread, #49).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from astrotool_core.acquisition.fresh_frame_after_motion import FreshFrameBudget, FreshFrameSource
from astrotool_core.acquisition.stable_frame_acquisition import (
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.port import FocuserPort
from astrotool_core.focus.search_bounds import DEFAULT_ENVELOPE_STEPS
from astrotool_core.focus.star_focus_metric import StarTargetTracker, measure_star_focus
from astrotool_core.focus.terrestrial_focus_metric import (
    TerrestrialFocusEvidence,
    analyze_terrestrial_focus,
)
from astrotool_core.timing import SYSTEM_CLOCK, Clock, poll_until

from collimation_tool.application.autofocus_search import (
    AutofocusStatus,
    BoundedFocusSearcher,
    BoundedSearchResult,
    FocusCurvePoint,
    FocusSample,
    InvalidSample,
)

GetFrame = Callable[[], "np.ndarray | None"]
WaitForFrame = Callable[[float, float], FrameAcquisitionResult]
SetAutoExposurePaused = Callable[[bool], None]

#: Bound and poll cadence of the best-effort return to P0 between exposure
#: attempts (unchanged values; named for the issue #53 migration).
_RETURN_SETTLE_TIMEOUT_S = 10.0
_RETURN_POLL_INTERVAL_S = 0.05

#: S6.7 (T05): the fresh-frame budget while the exposure is still unknown assumes 2 s
#: (2 x 2 s + 1 s = the previous fixed 5 s); see the module docstring.
AUTOFOCUS_FRESH_FRAME_BUDGET = FreshFrameBudget(assumed_exposure_s=2.0)

#: Issue #56 review F3: a terrestrial score is "clipping-dominated" when tiles with clipped
#: pixels carry more than this share of it. Above one half the clipped highlights -- whose
#: response to focus depends on how far above full scale they really are (unknowable from
#: clipped data; a highlight far above it can make the score RISE with defocus) -- outweigh
#: all unclipped detail together, so the curve can no longer be trusted. Measured on the real
#: frames: 0.75-0.80 (diagnostic 402eabde), 0.91-0.93 (f0bc314b) -- both stepped down.
CLIPPED_SHARE_LIMIT = 0.5
#: `failure_reason` of a terrestrial run whose result is still clipping-dominated after the
#: exposure step-down (status INSUFFICIENT_EVIDENCE, never a bare SUCCESS).
HIGHLIGHTS_CLIPPED = "highlights_clipped"
#: The search statuses that report a focus curve -- the only ones clipped highlights can have
#: drawn; every other status (a refused move, a dead stream, ...) is reported unchanged.
_CURVE_STATUSES = frozenset(
    {
        AutofocusStatus.SUCCESS,
        AutofocusStatus.BEST_AT_SEARCH_LIMIT,
        AutofocusStatus.INCONSISTENT_CURVE,
    }
)


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
        fresh_frame_budget: FreshFrameBudget = AUTOFOCUS_FRESH_FRAME_BUDGET,
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
        clock: Clock | None = None,
        exposure_hint: Callable[[], float | None] | None = None,
    ) -> None:
        #: Issue #53: every settle wait, move-completion poll and frame
        #: reference time goes through this clock (default: the real one).
        self._clock = clock or SYSTEM_CLOCK
        self._exposure_control = exposure_control
        self._max_exposure_attempts = max_exposure_attempts
        self._tracker_max_shift_px = tracker_max_shift_px
        self._focuser = focuser
        self._get_frame = get_frame
        self._set_auto_exposure_paused = set_auto_exposure_paused
        self._camera_label = camera_label
        self._focuser_label = focuser_label
        #: S6.7: one fresh-frame source per controller (a run), learning the exposure.
        self._cancel_check: Callable[[], bool] | None = None
        #: S6.6b: `exposure_hint` = the camera exposure (s) the GUI thread snapshotted when the
        #: run started, so even the first frame wait is scaled for a long exposure.
        self._frames = FreshFrameSource(
            wait_for_frame,
            budget=fresh_frame_budget,
            exposure_hint=exposure_hint,
            clock=self._clock,
        )
        self._terrestrial_sample_count = max(1, terrestrial_sample_count)
        #: Issue #56 review F3: clipped share of each terrestrial score of the current run.
        self._clipped_shares: dict[float, float] = {}
        #: Review 2 #5: an exposure lowering was issued this run -> always restore.
        self._exposure_lowered = False
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
        self,
        mode: AutofocusMode,
        cancel_check: Callable[[], bool] | None = None,
        *,
        cancel_event: threading.Event | None = None,
    ) -> AutofocusResult:
        """`cancel_event` (S6.3): the caller's Stop event -- also ends the exposure settle
        wait in progress (the run then ends CANCELLED). Without `cancel_check`, the event
        is the cancel check too."""
        if cancel_check is None and cancel_event is not None:
            cancel_check = cancel_event.is_set
        # S6.7 review: a Stop also ends a long fresh-frame wait (within one wait slice).
        self._cancel_check = cancel_check
        self._set_auto_exposure_paused(True)
        artificial = mode is AutofocusMode.ARTIFICIAL_STAR
        terrestrial = mode is AutofocusMode.TERRESTRIAL
        control = self._exposure_control if (artificial or terrestrial) else None
        original = control.get() if control is not None else None
        attempts: list[tuple[float, int]] = [original] if original is not None else []
        tracker = StarTargetTracker(max_shift_px=self._tracker_max_shift_px)
        override: tuple[AutofocusStatus, str | None] | None = None
        self._clipped_shares = {}
        self._exposure_lowered = False
        try:
            if terrestrial and control is not None:
                # Issue #56 review F3: clipped highlights carrying the score can drive the
                # search off focus -- step the exposure down at the start position first.
                self._terrestrial_exposure_precheck(control, attempts, cancel_event)
            steps_left = self._max_exposure_attempts - len(attempts)
            for _attempt in range(1 + max(0, steps_left) if control is not None else 1):
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
                    clock=self._clock,
                )
                measure = self._build_measurer(mode, tracker)
                search_result = searcher.search(measure, cancel_check=cancel_check)
                if artificial:
                    retry = any(point.reason == "saturated" for point in search_result.samples)
                else:
                    # Review 2 #1: only a search that drew a curve can have been drawn by
                    # clipped highlights -- a refused move or a dead stream is reported as is.
                    retry = (
                        terrestrial
                        and search_result.status in _CURVE_STATUSES
                        and self._clipping_dominated(search_result)
                    )
                if (
                    control is None
                    or not retry
                    or search_result.status is AutofocusStatus.CANCELLED
                    or len(attempts) >= self._max_exposure_attempts
                ):
                    break
                # Saturated (artificial star) / clipping-dominated (terrestrial) at or on the
                # way to focus: lower the exposure, return to the original P0 (so the
                # +-envelope is unchanged) and search again below full scale.
                lowered = self._lower_exposure(control, attempts, cancel_event)
                if lowered is not None:
                    if lowered == "cancelled":
                        override = (AutofocusStatus.CANCELLED, None)
                    elif artificial:
                        override = (AutofocusStatus.NO_USABLE_EVIDENCE, "saturated_at_min_exposure")
                    break
                self._return_to(search_result.start_position)
            if (
                override is None
                and terrestrial
                and search_result.status in _CURVE_STATUSES
                and self._clipping_dominated(search_result)
            ):
                # Never a bare SUCCESS on a curve the clipped highlights drew (review F3);
                # never replacing a failure status (review 2 #1). The best position is not
                # trusted: go back to where the run started, as the searcher does for its own
                # insufficient evidence (review 2 #4).
                override = (AutofocusStatus.INSUFFICIENT_EVIDENCE, HIGHLIGHTS_CLIPPED)
                self._return_to(search_result.start_position)
        finally:
            # Review 2 #5: restore whenever a lowering was ISSUED -- the camera may apply it
            # later (queued to the UI thread), so reading the value back can still show the
            # original and skip the restore.
            if control is not None and original is not None and self._exposure_lowered:
                control.set(*original)
            self._set_auto_exposure_paused(False)

        valid = [point for point in search_result.samples if point.valid]
        confidence = valid[-1].confidence if valid else 0.0
        status = search_result.status
        failure_reason = search_result.failure_reason
        if override is not None:
            status, failure_reason = override
        elif (
            status in (AutofocusStatus.FRAME_ACQUISITION_FAILED, AutofocusStatus.NO_USABLE_EVIDENCE)
            and failure_reason is None
            and (frame_reason := self._frame_failure_reason()) is not None
        ):
            # S6.7: the last fresh-frame wait produced no frame -- say so (also when it was the
            # very first measurement, which the searcher reports as no usable evidence); a
            # Stop that ended the wait is a cancellation.
            if frame_reason == "cancelled":
                status = AutofocusStatus.CANCELLED
            else:
                failure_reason = frame_reason
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

    def _frame_failure_reason(self) -> str | None:
        """S6.7: why the last fresh-frame wait produced no frame -- explicit, e.g.
        'fresh_frame_timeout: no frame arrived (waited 5.0 s of 5.0 s; exposure unknown)'."""
        last = self._frames.last_result
        if last is None or last.ok:
            return None
        if last.status is FrameAcquisitionStatus.CANCELLED:
            return "cancelled"
        code = "fresh_frame_timeout" if last.timed_out else last.status.value
        return f"{code}: {last.describe()}"

    def _return_to(self, position: int) -> None:
        """Best-effort return to the original start position between
        exposure attempts (same bounds as the search itself: it is P0)."""
        self._focuser.move_absolute(position)
        poll_until(
            lambda: not self._focuser.is_moving(),
            timeout_s=_RETURN_SETTLE_TIMEOUT_S, interval_s=_RETURN_POLL_INTERVAL_S,
            clock=self._clock,
        )

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
        # The searcher has already waited for the focuser move to complete: it ended now.
        acquisition = self._frames.after_motion(cancelled=self._cancel_check)
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

    def _terrestrial_samples(self) -> list[np.ndarray] | None:
        """Issue #56 review F2: `terrestrial_sample_count` DISTINCT fresh frames -- each one
        exposed after the previous arrived (the fresh-frame source, not a `get_frame()`
        re-read of the same latest frame), so the metric's motion test and noise floor see
        real independent samples. A re-delivered identical frame is dropped; a failed extra
        wait just ends the series (the first frame alone is still a measurement)."""
        first = self._acquire_fresh_frame()
        if first is None:
            return None
        frames: list[np.ndarray] = [first]
        for _ in range(self._terrestrial_sample_count - 1):
            extra = self._acquire_fresh_frame()
            if extra is None:
                last = self._frames.last_result
                if last is not None and last.status is FrameAcquisitionStatus.CANCELLED:
                    return None
                break
            if not any(np.array_equal(extra, seen) for seen in frames):
                frames.append(extra)
        return frames

    def _terrestrial_evidence(self) -> TerrestrialFocusEvidence | None:
        frames = self._terrestrial_samples()
        if frames is None:
            return None
        return analyze_terrestrial_focus(frames, tile_size_px=self._terrestrial_tile_size_px)

    def _measure_terrestrial(self) -> FocusSample | None:
        evidence = self._terrestrial_evidence()
        if evidence is None:
            return None
        measurement = evidence.measurement
        if measurement.sharpness is None:
            return None
        self._clipped_shares[measurement.sharpness] = evidence.clipped_share
        return FocusSample(value=measurement.sharpness, confidence=measurement.confidence)

    def _clipping_dominated(self, search_result: BoundedSearchResult) -> bool:
        """Clipped tiles carry the score of a sample that competed for best focus: any valid
        sample scoring within the improvement margin of the best, or measured within one
        coarse step of the best position (review 2 #2: the true focus can be the clipped
        sample a just-unclipped neighbour narrowly outscores)."""
        valid = [point for point in search_result.samples if point.valid]
        if not valid:
            return False
        best_value = max(point.value for point in valid)
        best_position = search_result.best_position
        for point in valid:
            competing = point.value >= best_value * (1.0 - self._improvement_fraction) or (
                best_position is not None
                and abs(point.position - best_position) <= self._coarse_step
            )
            if competing and self._clipped_shares.get(point.value, 0.0) > CLIPPED_SHARE_LIMIT:
                return True
        return False

    def _lower_exposure(
        self,
        control: ExposureControl,
        attempts: list[tuple[float, int]],
        cancel_event: threading.Event | None,
    ) -> str | None:
        """One exposure step-down: None when applied (recorded in `attempts`), else
        'cancelled' (Stop during the settle) or 'at_min_exposure' (the camera clamped it)."""
        exposure_ms, gain = control.get()
        self._exposure_lowered = True  # before the call: a queued setter may apply it later
        control.set(exposure_ms * control.factor, gain)
        if not self._clock.sleep(control.settle_s, cancel_event):
            return "cancelled"  # S6.3: Stop during the settle -- nothing further is searched.
        new_ms, new_gain = control.get()
        if new_ms >= exposure_ms * 0.999:
            return "at_min_exposure"
        attempts.append((new_ms, new_gain))
        return None

    def _terrestrial_exposure_precheck(
        self,
        control: ExposureControl,
        attempts: list[tuple[float, int]],
        cancel_event: threading.Event | None,
    ) -> None:
        """Before searching: while clipped tiles carry the score at the start position, lower
        the exposure (up to `max_exposure_attempts` in total). A Stop or the camera's minimum
        ends the step-down; the search then runs (and reports cancellation / clipping)."""
        while len(attempts) < self._max_exposure_attempts:
            if self._cancel_check is not None and self._cancel_check():
                return
            evidence = self._terrestrial_evidence()
            if evidence is None or evidence.clipped_share <= CLIPPED_SHARE_LIMIT:
                return
            if self._lower_exposure(control, attempts, cancel_event) is not None:
                return
