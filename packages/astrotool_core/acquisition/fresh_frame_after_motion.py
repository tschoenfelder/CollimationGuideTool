"""The fresh-frame-after-motion rule (S6.7, #52; #43/#30/#39) -- its ONE owner.

AGENTS.md: "Frames captured during movement may be displayed but must never be used as
measurement evidence." Every measurement taken after a commanded motion (a Mount Align
calibration step, a reacquisition correction, an autofocus focuser move) therefore uses a frame
whose exposure STARTED at or after

    measurement_reference(motion_ended_at, settle_s) = motion_ended_at + settle_s

and waits for it under an EXPOSURE-SCALED deadline with an explicit outcome:

- `FreshFrameBudget.timeout_s(exposure_s)` -- the wait may need the exposure that was already
  running when the motion ended (it overlaps the motion and is rejected) plus one complete fresh
  exposure, so the budget is `max(floor_s, exposures * exposure_s + margin_s)` (2 s floor, 2
  exposures, 1 s margin -- the values Mount Align has used since diagnostic 9ca6daa3). A fixed
  budget made every exposure longer than ~2 s time out (T05: autofocus' fixed 5 s).
- the deadline is anchored at `max(now, reference)`: a reference still in the future (the
  settle) never eats into the budget.
- when the exposure is not known up front, the budget starts from `budget.assumed_exposure_s`
  and is extended as soon as a delivered frame reveals a longer exposure
  (`FrameAcquisitionResult.longest_exposure_s`), so a long exposure is waited for instead of
  timing out, while a dead camera still ends at the deadline.
- `FreshFrameResult.status` is explicit: OK, TIMEOUT (nothing arrived), EXPOSURE_OVERLAPPED_MOTION
  (frames arrived but every one started before the reference), CAMERA_UNAVAILABLE, CANCELLED.

Built on the existing `acquire_stable_frame` decision (the per-frame exposure-start check, via
any `StableFrameWaiter`, e.g. `CameraPanel.wait_for_frame_after`) and the injected `Clock`. It
never moves anything and knows nothing about mounts, focusers or Qt. Callers: reacquisition
(`collimation_tool.application.star_acquisition`), autofocus (`autofocus_controller`), and Mount
Align's stability-verified captures, which use `measurement_reference` and `DEFAULT_FRESH_FRAME_
BUDGET.timeout_s` for each sample of their window (`motion_aware_acquisition`).

Manual nudges are positioning actions, not measurements (AGENTS.md "Manual movement versus
measurement"): this rule applies to the measurement that may FOLLOW a motion, never to the
motion itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionStatus,
    StableFrameWaiter,
)
from astrotool_core.timing import SYSTEM_CLOCK, Clock

#: The longest exposure a measurement after motion is waited for (S6.7 review). Live view's
#: auto-exposure stops at 2 s (`AutoExposureConfig.max_auto_exposure_ms`) and every measured
#: exposure seen in field diagnostics was <= 2 s; 30 s is 15x that headroom. A longer exposure
#: (the cameras allow up to 60 s) ends the wait as an explicit timeout instead of blocking a
#: worker -- and the operator's Stop -- for minutes.
MAX_MEASUREMENT_EXPOSURE_S = 30.0


@dataclass(frozen=True)
class FreshFrameBudget:
    """How long one fresh-frame wait may take, scaled by the camera's exposure."""

    #: Never less than this, however short the exposure.
    floor_s: float = 2.0
    #: The in-flight exposure (rejected: it overlapped the motion) plus one complete fresh one.
    exposures: float = 2.0
    #: Delivery/readout latency on top of the exposures.
    margin_s: float = 1.0
    #: The exposure assumed while none is known (no hint, no frame seen yet); None = only the
    #: floor. A delivered frame showing a longer exposure extends the budget either way.
    assumed_exposure_s: float | None = None
    #: Absolute cap on one wait, however long the exposure (2 x 30 s + 1 s = 61 s).
    max_timeout_s: float = 2.0 * MAX_MEASUREMENT_EXPOSURE_S + 1.0
    #: The waiter is called for at most this long at a time, so `cancelled()` (Stop) is seen
    #: within one slice instead of after the whole budget.
    slice_s: float = 0.5

    def timeout_s(self, exposure_s: float | None) -> float:
        """The wait budget for a camera exposing `exposure_s` (None = unknown), capped."""
        if exposure_s is None:
            exposure_s = self.assumed_exposure_s
        if exposure_s is None:
            return min(self.floor_s, self.max_timeout_s)
        scaled = max(self.floor_s, self.exposures * max(0.0, exposure_s) + self.margin_s)
        return min(scaled, self.max_timeout_s)


#: The production budget (Mount Align's long-standing numbers, now owned here).
DEFAULT_FRESH_FRAME_BUDGET = FreshFrameBudget()


def measurement_reference(motion_ended_at: float, settle_s: float = 0.0) -> float:
    """The earliest exposure START a measurement after a motion may use."""
    return motion_ended_at + max(0.0, settle_s)


#: Statuses that mean "no fresh frame in time" (the explicit timeout outcome).
TIMEOUT_STATUSES = frozenset(
    {
        FrameAcquisitionStatus.TIMEOUT,
        FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION,
        FrameAcquisitionStatus.SETTLE_NOT_REACHED,
    }
)

_DESCRIPTIONS = {
    FrameAcquisitionStatus.OK: "fresh frame",
    FrameAcquisitionStatus.TIMEOUT: "no frame arrived",
    FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION: (
        "every frame's exposure started before the motion had ended and settled"
    ),
    FrameAcquisitionStatus.SETTLE_NOT_REACHED: "no frame after the settle",
    FrameAcquisitionStatus.CAMERA_UNAVAILABLE: "camera not available",
    FrameAcquisitionStatus.CANCELLED: "cancelled",
}


@dataclass(frozen=True)
class FreshFrameResult:
    """The outcome of one fresh-frame wait -- never a silent stale frame."""

    status: FrameAcquisitionStatus
    frame: DeliveredFrame | None
    #: The exposure-start bound the frame had to meet.
    reference_monotonic: float
    #: The budget the wait ended with (exposure-scaled, possibly extended).
    timeout_s: float
    #: The exposure the budget was scaled from (None = unknown, floor/assumed used).
    exposure_s: float | None
    #: Clock time spent waiting.
    waited_s: float

    @property
    def ok(self) -> bool:
        return self.status is FrameAcquisitionStatus.OK and self.frame is not None

    @property
    def timed_out(self) -> bool:
        return self.status in TIMEOUT_STATUSES

    def describe(self) -> str:
        what = _DESCRIPTIONS.get(self.status, self.status.value)
        exposure = "unknown" if self.exposure_s is None else f"{self.exposure_s:g} s"
        return (
            f"{what} (waited {self.waited_s:.1f} s of {self.timeout_s:.1f} s; "
            f"exposure {exposure})"
        )


def acquire_fresh_frame_after_motion(
    waiter: StableFrameWaiter,
    *,
    motion_ended_at: float,
    settle_s: float = 0.0,
    exposure_s: float | None = None,
    budget: FreshFrameBudget = DEFAULT_FRESH_FRAME_BUDGET,
    cancelled: Callable[[], bool] | None = None,
    clock: Clock | None = None,
) -> FreshFrameResult:
    """One frame whose exposure started at or after `motion_ended_at + settle_s`, waited for
    under the exposure-scaled budget (see the module docstring). `exposure_s` is the camera's
    current exposure when the caller knows it; a longer one seen on a delivered frame extends
    the budget (never beyond `budget.max_timeout_s`). The waiter is called in slices of at
    most `budget.slice_s`; `cancelled()` is checked before each, so a Stop ends the wait within
    one slice."""
    source_clock = clock or SYSTEM_CLOCK
    is_cancelled = cancelled or (lambda: False)
    reference = measurement_reference(motion_ended_at, settle_s)
    started = source_clock.monotonic()
    anchor = max(started, reference)
    known = exposure_s
    timeout = budget.timeout_s(known)

    def finish(status: FrameAcquisitionStatus, frame: DeliveredFrame | None) -> FreshFrameResult:
        return FreshFrameResult(
            status, frame, reference, timeout, known, source_clock.monotonic() - started
        )

    status = FrameAcquisitionStatus.TIMEOUT
    while True:
        if is_cancelled():
            return finish(FrameAcquisitionStatus.CANCELLED, None)
        remaining = anchor + timeout - source_clock.monotonic()
        if remaining <= 0:
            return finish(status, None)
        result = waiter(reference, min(remaining, budget.slice_s))
        if result.ok and result.frame is not None:
            if known is None or result.frame.exposure_seconds > known:
                known = result.frame.exposure_seconds
            return finish(FrameAcquisitionStatus.OK, result.frame)
        if result.status not in TIMEOUT_STATUSES:
            return finish(result.status, None)
        # Keep the most informative timeout reason: once a frame overlapping the motion was
        # seen, "overlapped" stays the answer even if a later wait saw nothing at all.
        if status is not FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION:
            status = result.status
        seen = result.longest_exposure_s
        if seen is not None and (known is None or seen > known):
            known = seen
            timeout = max(timeout, budget.timeout_s(known))
        # Otherwise this slice simply ended: keep waiting until the deadline.


class FreshFrameSource:
    """`acquire_fresh_frame_after_motion` bound to one camera for a workflow run: remembers the
    exposure of the last fresh frame, so later waits are scaled from the start. `exposure_hint`
    (e.g. the camera panel's current exposure) wins over the remembered value when given."""

    def __init__(
        self,
        waiter: StableFrameWaiter,
        *,
        settle_s: float = 0.0,
        budget: FreshFrameBudget = DEFAULT_FRESH_FRAME_BUDGET,
        exposure_hint: Callable[[], float | None] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._waiter = waiter
        self._settle_s = settle_s
        self._budget = budget
        self._exposure_hint = exposure_hint
        self._clock = clock or SYSTEM_CLOCK
        self._learned_exposure_s: float | None = None
        #: The most recent outcome (None before the first wait).
        self.last_result: FreshFrameResult | None = None

    def _exposure(self) -> float | None:
        if self._exposure_hint is not None:
            hinted = self._exposure_hint()
            if hinted is not None:
                return hinted
        return self._learned_exposure_s

    def after_motion(
        self, motion_ended_at: float | None = None, *, cancelled: Callable[[], bool] | None = None
    ) -> FreshFrameResult:
        """A fresh frame after a motion that ended at `motion_ended_at` (None = now: the
        caller's blocking motion call has just returned)."""
        ended = self._clock.monotonic() if motion_ended_at is None else motion_ended_at
        result = acquire_fresh_frame_after_motion(
            self._waiter,
            motion_ended_at=ended,
            settle_s=self._settle_s,
            exposure_s=self._exposure(),
            budget=self._budget,
            cancelled=cancelled,
            clock=self._clock,
        )
        if result.ok and result.frame is not None:
            self._learned_exposure_s = result.frame.exposure_seconds
        self.last_result = result
        return result
