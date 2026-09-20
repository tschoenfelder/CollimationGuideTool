"""Bounded, metric-agnostic autofocus search — issue #33's own safety-
critical core. Reuses `focus_controller.FocusSearcher`'s proven *shape*
(symmetric two-direction probe -> coarse hill-climb -> backtrack to best
-> consistent-direction final approach) but is a new, independent class:
`FocusSearcher` itself stays byte-for-byte unchanged (its own 7 tests must
keep passing -- this project's own established convention is additive,
not refactor-in-place, for reviewed/tested code, see issue #31's
`axis_calibration.py` additions) since the two now differ enough in
contract (hard bounds, a full recorded curve, an explicit result-status
taxonomy, metric-direction-agnostic) that forcing a shared base would add
abstraction without payoff for a first version.

Unlike `FocusSearcher` (relative `move()` only, "never trusts absolute
focuser position"), this searcher commands `move_absolute()` for every
step -- `move()` returns no accept/reject signal at all
(`FocuserPort.move`'s own docstring), while `move_absolute()` does
(`FocuserMoveResult.accepted`), and issue #33 explicitly wants a real
"rejected focuser command aborts cleanly" test. Positions are still never
trusted from the *device's* own drifting absolute readback beyond the one
initial `get_position()` read that establishes `P0` -- every subsequent
position this class reasons about is the target it itself just commanded.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from astrotool_core.focus.port import FocuserPort
from astrotool_core.focus.search_bounds import (
    DEFAULT_ENVELOPE_STEPS,
    FocuserSearchBounds,
    compute_search_bounds,
)


class AutofocusStatus(Enum):
    """Issue #33's own "Result / confidence semantics" list. Not every
    member is necessarily produced by this searcher alone yet --
    `INSUFFICIENT_EVIDENCE`/`INCONSISTENT_CURVE`/`DEVICE_LIMIT_REACHED`
    are reserved for the mode-specific analyzers/controller layered on
    top (same "define the full taxonomy, only some members are produced
    yet" precedent as `RegistrationStatus.INSUFFICIENT_STARS`)."""

    SUCCESS = "success"
    CANCELLED = "cancelled"
    NO_USABLE_EVIDENCE = "no_usable_evidence"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    FOCUSER_UNAVAILABLE = "focuser_unavailable"
    MOVE_REJECTED = "move_rejected"
    FRAME_ACQUISITION_FAILED = "frame_acquisition_failed"
    BEST_AT_SEARCH_LIMIT = "best_at_search_limit"
    INCONSISTENT_CURVE = "inconsistent_curve"
    DEVICE_LIMIT_REACHED = "device_limit_reached"
    #: Issue #33 (artificial star): the start position was already the
    #: optimum -- a fresh validation frame matches the start within the
    #: improvement margin. Explicitly NOT `SUCCESS` (no improvement was
    #: made or claimed) and NOT a failure.
    ALREADY_FOCUSED = "already_focused"


@dataclass(frozen=True)
class FocusSample:
    """One mode-analyzer measurement at the focuser's *current* position --
    `value` in whatever unit that mode uses (FWHM px for star mode,
    Tenengrad ratio for terrestrial), `confidence` in `[0, 1]`."""

    value: float
    confidence: float = 1.0


@dataclass(frozen=True)
class InvalidSample:
    """Issue #33 (artificial star): the target could not be measured at
    this position (`saturated`, `donut_like`, `not_found_at_tracked_position`,
    ...). With `allow_invalid_samples` the searcher records it and moves on;
    it is never replaced by a measurement of some other feature."""

    reason: str


FocusMeasurer = Callable[[], "FocusSample | InvalidSample | None"]


@dataclass(frozen=True)
class FocusCurvePoint:
    position: int
    value: float
    confidence: float
    #: False for an `InvalidSample` (value is NaN, `reason` says why).
    valid: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class BoundedSearchResult:
    status: AutofocusStatus
    start_position: int
    best_position: int | None
    search_min: int | None
    search_max: int | None
    samples: tuple[FocusCurvePoint, ...] = field(default_factory=tuple)
    final_value: float | None = None
    start_value: float | None = None
    failure_reason: str | None = None


class _SearchAbort(Exception):
    """Internal control-flow signal only -- never escapes `search()`.
    Raised by `_move_to`/`_measure_at` the moment a move is rejected or a
    measurement comes back empty, so each search phase (`_probe`/
    `_hill_climb`) can read as its own straight-line logic instead of
    threading a tri-state (accepted, sample-or-None) result through every
    call site by hand."""

    def __init__(self, status: AutofocusStatus, best_position: int | None) -> None:
        super().__init__(status)
        self.status = status
        self.best_position = best_position


@dataclass
class _Run:
    """Mutable state for one `search()` call -- passed to each phase
    method rather than closed over, so those phases are plain,
    independently-readable methods instead of nested closures.

    `start_value` (issue #35) is the very first measurement, at
    `start_position` -- set once, never mutated, distinct from
    `best_value` (which does get overwritten as climbing improves) so a
    final validation step can still compare against where the search
    actually began."""

    start_position: int
    bounds: FocuserSearchBounds
    best_value: float
    best_position: int
    current_pos: int
    start_value: float = 0.0
    step: int = 0
    samples: list[FocusCurvePoint] = field(default_factory=list)


class BoundedFocusSearcher:
    """Coarse-to-fine hill-climb bounded to `P0 ± envelope_steps`
    intersected with the device's own limits -- see `search_bounds`'s own
    module docstring for why `device_min_position` defaults to `0`."""

    def __init__(
        self,
        focuser: FocuserPort,
        *,
        higher_is_better: bool,
        coarse_step: int = 250,
        fine_step: int = 25,
        max_coarse_steps: int = 20,
        max_consecutive_no_improve: int = 2,
        improvement_fraction: float = 0.05,
        final_approach_direction: int = 1,
        envelope_steps: int = DEFAULT_ENVELOPE_STEPS,
        device_min_position: int = 0,
        move_settle_timeout_s: float = 10.0,
        move_poll_interval_s: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.monotonic,
        allow_invalid_samples: bool = False,
        require_improvement: bool = False,
    ) -> None:
        self._allow_invalid = allow_invalid_samples
        self._require_improvement = require_improvement
        self._focuser = focuser
        self._higher_is_better = higher_is_better
        self._coarse_step = coarse_step
        self._fine_step = fine_step
        self._max_coarse_steps = max_coarse_steps
        self._max_consecutive_no_improve = max_consecutive_no_improve
        self._improvement_fraction = improvement_fraction
        self._final_dir = final_approach_direction
        self._envelope_steps = envelope_steps
        self._device_min_position = device_min_position
        self._move_settle_timeout_s = move_settle_timeout_s
        self._move_poll_interval_s = move_poll_interval_s
        self._sleep = sleep
        self._now = now

    def search(
        self,
        measure: FocusMeasurer,
        cancel_check: Callable[[], bool] | None = None,
    ) -> BoundedSearchResult:
        if not self._focuser.is_available:
            return BoundedSearchResult(
                status=AutofocusStatus.FOCUSER_UNAVAILABLE,
                start_position=0, best_position=None, search_min=None, search_max=None,
            )

        start_position = self._focuser.get_position()
        bounds = compute_search_bounds(
            start_position, self._focuser.get_max_position(),
            device_min_position=self._device_min_position, envelope_steps=self._envelope_steps,
        )

        initial = measure()
        if not isinstance(initial, FocusSample):
            reason = initial.reason if isinstance(initial, InvalidSample) else None
            samples = (
                (FocusCurvePoint(start_position, float("nan"), 0.0, False, reason),)
                if reason is not None
                else ()
            )
            return BoundedSearchResult(
                status=AutofocusStatus.NO_USABLE_EVIDENCE, start_position=start_position,
                best_position=None, search_min=bounds.allowed_min, search_max=bounds.allowed_max,
                samples=samples, failure_reason=reason,
            )

        run = _Run(
            start_position=start_position, bounds=bounds, best_value=initial.value,
            best_position=start_position, current_pos=start_position,
            start_value=initial.value, step=self._coarse_step,
        )
        self._record(run, start_position, initial)

        try:
            return self._search_from_probe(run, measure, cancel_check)
        except _SearchAbort as abort:
            if abort.status is not AutofocusStatus.MOVE_REJECTED:
                self._safe_return_to(run, run.best_position)
            return self._make_result(run, abort.status, None)

    def _search_from_probe(
        self, run: _Run, measure: FocusMeasurer, cancel_check: Callable[[], bool] | None
    ) -> BoundedSearchResult:
        direction = self._probe(run, measure)
        if direction == 0:
            return self._finish_flat(run, measure)

        self._hill_climb(run, measure, direction, cancel_check)
        # Issue #35 Fix 1: captured from hill-climb's own last sample,
        # *before* _final_approach appends its own confirmation sample
        # (which would otherwise overwrite run.samples[-1]) -- see this
        # method's own "reached the boundary while still rising" check
        # below for why the bookkept best_position alone isn't enough.
        reached_boundary_while_rising = self._reached_boundary_while_rising(run)
        final_value = self._final_approach(run, measure, direction)

        # Issue #35 Fix 2: a fresh, post-move confirmation sample that's
        # decisively worse than where the search started (e.g. backlash/
        # hysteresis making the real resting position worse than
        # exploratory samples suggested) must never be reported as
        # success -- return to the one position already proven good
        # (start_position) instead.
        if final_value is not None and self._better(run.start_value, final_value):
            self._move_to(run, run.start_position)
            return self._make_result(run, AutofocusStatus.INCONSISTENT_CURVE, final_value)
        if self._require_improvement:
            # Issue #33 (artificial star): success needs a fresh, validated
            # frame that is genuinely better than where the run started.
            if final_value is None:
                self._move_to(run, run.start_position)
                return self._make_result(run, AutofocusStatus.INSUFFICIENT_EVIDENCE, None)
            if not self._better(final_value, run.start_value):
                self._move_to(run, run.start_position)
                return self._make_result(run, AutofocusStatus.INCONSISTENT_CURVE, final_value)

        status = AutofocusStatus.SUCCESS
        if (
            run.best_position in (run.bounds.allowed_min, run.bounds.allowed_max)
            or reached_boundary_while_rising
        ):
            status = AutofocusStatus.BEST_AT_SEARCH_LIMIT
        return self._make_result(run, status, final_value)

    def _reached_boundary_while_rising(self, run: _Run) -> bool:
        """Issue #35: the real diagnostic bundle showed a curve that
        never turned over before hitting the search boundary, yet
        `best_position in (allowed_min, allowed_max)` alone missed it --
        `_better()`'s 5% margin can leave `best_position` one coarse-step
        *behind* the literal boundary even though the last sample taken
        there was still (barely) rising, not declining. Checks hill-
        climb's own last sample directly: at the boundary, and not
        confirmed to have declined from the recorded best."""
        if not run.samples:
            return False
        last = run.samples[-1]
        at_boundary = last.position in (run.bounds.allowed_min, run.bounds.allowed_max)
        declined = self._better(run.best_value, last.value)
        return at_boundary and not declined

    def _finish_flat(self, run: _Run, measure: FocusMeasurer) -> BoundedSearchResult:
        # Neither probe direction improved on the starting position's own
        # measurement -- a real, successful outcome (already at/near the
        # local optimum), not "no evidence": every measurement taken so
        # far was genuinely valid, it just never beat the start.
        self._move_to(run, run.start_position)
        final_sample = measure()
        valid_final = final_sample if isinstance(final_sample, FocusSample) else None
        final_value = valid_final.value if valid_final is not None else run.best_value
        if valid_final is not None:
            self._record(run, run.start_position, valid_final)
        if self._require_improvement:
            if valid_final is None:
                return self._make_result(run, AutofocusStatus.INSUFFICIENT_EVIDENCE, None)
            if self._better(run.start_value, final_value):
                return self._make_result(run, AutofocusStatus.INCONSISTENT_CURVE, final_value)
            return self._make_result(run, AutofocusStatus.ALREADY_FOCUSED, final_value)
        return self._make_result(run, AutofocusStatus.SUCCESS, final_value)

    def _probe(self, run: _Run, measure: FocusMeasurer) -> int:
        """Tests both directions once from the start; returns +1/-1 (that
        direction improved) or 0 (neither did). With `allow_invalid_samples`
        (issue #33, artificial star) it also probes both sides at the fine
        step before giving up: the sharp zone can be narrower than the
        coarse step, and a valid-but-worse coarse neighbour (or an invalid
        donut one) says nothing about the start's own vicinity."""
        steps = [self._coarse_step]
        if self._allow_invalid and self._fine_step < self._coarse_step:
            steps.append(self._fine_step)
        for step in steps:
            for sign in (1, -1):
                pos = self._move_to(run, run.start_position + sign * step)
                run.current_pos = pos
                sample = self._measure_at(run, measure, pos)
                if sample is not None and self._better(sample.value, run.best_value):
                    run.best_value, run.best_position, run.step = sample.value, pos, step
                    return sign
        return 0

    def _hill_climb(
        self, run: _Run, measure: FocusMeasurer, direction: int,
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        consecutive_no_improve = 0
        for _ in range(self._max_coarse_steps):
            if cancel_check is not None and cancel_check():
                raise _SearchAbort(AutofocusStatus.CANCELLED, run.best_position)

            run.current_pos = self._move_to(run, run.current_pos + direction * run.step)
            sample = self._measure_at(run, measure, run.current_pos)

            if sample is not None and self._better(sample.value, run.best_value):
                run.best_value, run.best_position = sample.value, run.current_pos
                consecutive_no_improve = 0
            else:
                consecutive_no_improve += 1
                if consecutive_no_improve >= self._max_consecutive_no_improve:
                    return

    def _final_approach(
        self, run: _Run, measure: FocusMeasurer, direction: int
    ) -> float | None:
        """Backtracks to the best position found, then -- issue #33's own
        backlash requirement -- makes sure the very last physical move
        into it comes from the configured `final_approach_direction`,
        regardless of which way the hill-climb itself scanned."""
        self._move_to(run, run.best_position)
        if direction != self._final_dir:
            self._move_to(run, run.best_position - self._final_dir * self._fine_step)
            self._move_to(run, run.best_position)

        final_sample = measure()
        if not isinstance(final_sample, FocusSample):
            if isinstance(final_sample, InvalidSample):
                self._record_invalid(run, run.best_position, final_sample)
            return None
        self._record(run, run.best_position, final_sample)
        return final_sample.value

    def _better(self, candidate: float, baseline: float) -> bool:
        if self._higher_is_better:
            return candidate > baseline * (1.0 + self._improvement_fraction)
        return candidate < baseline * (1.0 - self._improvement_fraction)

    def _record(self, run: _Run, position: int, sample: FocusSample) -> None:
        run.samples.append(
            FocusCurvePoint(position=position, value=sample.value, confidence=sample.confidence)
        )

    def _record_invalid(self, run: _Run, position: int, sample: InvalidSample) -> None:
        run.samples.append(
            FocusCurvePoint(position, float("nan"), 0.0, valid=False, reason=sample.reason)
        )

    def _move_to(self, run: _Run, target: int) -> int:
        """Commands the bounds-clamped target; returns the actual
        resulting position. Raises `_SearchAbort(MOVE_REJECTED, ...)` if
        the focuser refuses."""
        clamped = run.bounds.clamp(target)
        if clamped == self._focuser.get_position() and run.samples:
            # Already there (a repeated clamp at a boundary) -- no real
            # move to issue or wait for.
            return clamped
        result = self._focuser.move_absolute(clamped)
        if not result.accepted:
            raise _SearchAbort(AutofocusStatus.MOVE_REJECTED, run.best_position)
        self._wait_for_move_settled()
        return clamped

    def _safe_return_to(self, run: _Run, position: int) -> None:
        """Best-effort recovery move -- swallows a further rejection
        rather than letting a recovery attempt itself raise."""
        with contextlib.suppress(_SearchAbort):
            self._move_to(run, position)

    def _measure_at(
        self, run: _Run, measure: FocusMeasurer, position: int
    ) -> FocusSample | None:
        """A valid sample, or `None` for a recorded `InvalidSample` (only
        when `allow_invalid_samples`; otherwise an unmeasurable sample
        aborts the run as before)."""
        sample = measure()
        if isinstance(sample, InvalidSample):
            if not self._allow_invalid:
                raise _SearchAbort(AutofocusStatus.FRAME_ACQUISITION_FAILED, run.best_position)
            self._record_invalid(run, position, sample)
            return None
        if sample is None:
            raise _SearchAbort(AutofocusStatus.FRAME_ACQUISITION_FAILED, run.best_position)
        self._record(run, position, sample)
        return sample

    def _wait_for_move_settled(self) -> None:
        deadline = self._now() + self._move_settle_timeout_s
        while self._focuser.is_moving() and self._now() < deadline:
            self._sleep(self._move_poll_interval_s)

    def _make_result(
        self, run: _Run, status: AutofocusStatus, final_value: float | None
    ) -> BoundedSearchResult:
        return BoundedSearchResult(
            status=status, start_position=run.start_position, best_position=run.best_position,
            search_min=run.bounds.allowed_min, search_max=run.bounds.allowed_max,
            samples=tuple(run.samples), final_value=final_value,
            start_value=run.start_value,
        )
