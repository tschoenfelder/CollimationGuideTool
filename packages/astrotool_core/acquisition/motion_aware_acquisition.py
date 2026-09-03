"""Motion-aware stable-frame acquisition — issue #30: extends issue #27's
`stable_frame_acquisition` (exposure-timing only: "was this frame's
exposure captured after a reference time?") with the physical-state
distinctions #30 requires: tracking vs. commanded movement, mechanical
settling as a *lower bound* rather than proof of stability, and an actual
measured image-stability check before a BEFORE or AFTER frame is trusted
for calibration.

Composes, rather than duplicates:

- `astrotool_core.mount.tracking_mode.ensure_tracking_mode` — the
  tracking-mode half (issue #30's "Tracking is not slewing").
- `astrotool_core.acquisition.stable_frame_acquisition`'s own
  `StableFrameWaiter` contract (e.g. `CameraPanel.wait_for_frame_after`
  bound per camera) — the exposure-timing half, unchanged from #27.
- `astrotool_core.acquisition.image_stability.check_image_stability` —
  the new measured-stability half, deliberately a distinct function from
  `measure_translation_offset` (the production calibration displacement
  estimator) even though both share that same low-level primitive
  underneath.

`acquire_verified_frame` answers "capture ONE calibration-valid frame
from this source, right now" (used for both the BEFORE and the AFTER
capture — issue #30's own "BEFORE-frame validity" section: the same
quality bar applies both times, not just post-motion). `acquire_verified_frames`
composes that over any number of named sources, mirroring #27's own
`acquire_settled_frames` camera-count-independence.

Deliberately NOT a literal state-machine class matching every named state
in the issue's own diagram (`PREPARE_MODE`/`MOTION_ACTIVE`/... — the
issue itself says "Names and exact classes are implementation choices").
Issuing the actual commanded mount movement, and deciding when to call
`ensure_tracking_mode` vs. this module's own capture functions, stays the
caller's job (`MountTestMovePanel`) — matching #27's own precedent of
never issuing mount commands from `astrotool_core.acquisition` itself.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from astrotool_core.acquisition.image_stability import StabilityCheckResult, check_image_stability
from astrotool_core.acquisition.stable_frame_acquisition import StableFrameWaiter


class MotionAwareStatus(Enum):
    #: A calibration-valid frame was captured -- its own exposure postdates
    #: the reference, and a short consecutive sequence around it measured
    #: within the configured stability tolerance.
    OK = "ok"
    #: The underlying #27 exposure-timing wait itself failed (never
    #: streaming, timed out, every delivered frame's exposure overlapped
    #: the reference) -- see `diagnostics["capture_status"]` for exactly
    #: which (a `FrameAcquisitionStatus` value).
    CAPTURE_INVALID = "capture_invalid"
    #: At least one full stability-check window was evaluated and never
    #: came in under tolerance before the deadline -- covers both issue
    #: #30's "still optically unstable after minimum settle" (called
    #: right after commanded movement) and its "wind prevents stable
    #: measurement" scenario (called with no commanded movement active at
    #: all, e.g. for a BEFORE frame) -- the same status either way, since
    #: this layer can't itself tell those apart; which call site produced
    #: it is the caller's own context.
    IMAGE_NOT_STABLE = "image_not_stable"
    #: The deadline was reached before even one full stability-check
    #: window could be gathered (e.g. a camera slower than the timeout
    #: budget) -- distinct from IMAGE_NOT_STABLE, which means a check
    #: actually ran and failed.
    SETTLE_TIMEOUT = "settle_timeout"
    #: The caller's own `cancelled()` callback fired mid-wait.
    CANCELLED = "cancelled"

    @property
    def ok(self) -> bool:
        return self is MotionAwareStatus.OK


@dataclass(frozen=True)
class CommandedMovementContext:
    """Diagnostics-only context about the commanded movement a BEFORE/AFTER
    capture pair straddles -- issue #30's own "Adaptive settling inputs"
    list. Never consulted for control flow by this module; purely carried
    through into `MotionAwareFrameResult.diagnostics` so a pulled bundle
    can correlate settle behavior with what actually moved, for a future
    adaptive settle model this issue explicitly defers building."""

    movement_type: str | None = None  # e.g. "pulse", "nudge", "slew"
    duration_ms: int | None = None
    rate_preset: str | None = None
    axis: str | None = None
    direction: str | None = None
    minimum_settle_ms: int | None = None
    temperature_c: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in (
                ("movement_type", self.movement_type),
                ("duration_ms", self.duration_ms),
                ("rate_preset", self.rate_preset),
                ("axis", self.axis),
                ("direction", self.direction),
                ("minimum_settle_ms", self.minimum_settle_ms),
                ("temperature_c", self.temperature_c),
            )
            if value is not None
        }


@dataclass(frozen=True)
class MotionAwareFrameResult:
    status: MotionAwareStatus
    frame: np.ndarray | None = None
    stability: StabilityCheckResult | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status.ok


def acquire_verified_frame(
    waiter: StableFrameWaiter,
    *,
    reference_monotonic: float,
    timeout_s: float,
    stability_tolerance_px: float,
    stability_sample_count: int = 3,
    stability_sample_interval_s: float = 0.2,
    movement_context: CommandedMovementContext | None = None,
    cancelled: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> MotionAwareFrameResult:
    """Repeatedly draws exposure-valid frames from `waiter` (issue #27's
    own `StableFrameWaiter` contract) into a sliding window of the most
    recent `stability_sample_count` samples, only returning `OK` once a
    full window measures within `stability_tolerance_px` of itself
    consecutive-pair-to-consecutive-pair (see
    `image_stability.check_image_stability`) -- issue #30's own central
    point: a fixed settle delay (`timeout_s`'s only role here is an
    *upper* bound, never evidence of stability on its own) is not proof
    the image has actually stopped moving.

    Each rejected window slides by exactly one sample (the oldest is
    dropped, `stability_sample_interval_s` elapses, then one fresh frame
    is drawn) rather than discarding the whole window and starting over
    -- keeps the check responsive without wasting already-good samples.
    `waiter`'s own reference is re-set to "now" before every fresh draw,
    so each new sample must itself be freshly captured, not a re-read of
    one already in the window.
    """
    is_cancelled = cancelled or (lambda: False)
    deadline = now() + timeout_s
    samples: list[np.ndarray] = []
    last_stability: StabilityCheckResult | None = None
    current_reference = reference_monotonic
    context_diagnostics = movement_context.as_dict() if movement_context is not None else {}

    while True:
        if is_cancelled():
            return MotionAwareFrameResult(
                MotionAwareStatus.CANCELLED, stability=last_stability,
                diagnostics=context_diagnostics,
            )
        remaining = deadline - now()
        if remaining <= 0:
            break
        result = waiter(current_reference, remaining)
        if not result.ok:
            return MotionAwareFrameResult(
                MotionAwareStatus.CAPTURE_INVALID, stability=last_stability,
                diagnostics={**context_diagnostics, "capture_status": result.status.value},
            )
        assert result.frame is not None
        samples.append(result.frame.pixels)
        if len(samples) > stability_sample_count:
            samples.pop(0)
        if len(samples) == stability_sample_count:
            last_stability = check_image_stability(
                samples, tolerance_px=stability_tolerance_px, min_samples=stability_sample_count
            )
            if last_stability.stable:
                return MotionAwareFrameResult(
                    MotionAwareStatus.OK, frame=samples[-1], stability=last_stability,
                    diagnostics=context_diagnostics,
                )
        sleep(stability_sample_interval_s)
        current_reference = now()

    status = (
        MotionAwareStatus.SETTLE_TIMEOUT
        if last_stability is None
        else MotionAwareStatus.IMAGE_NOT_STABLE
    )
    return MotionAwareFrameResult(status, stability=last_stability, diagnostics=context_diagnostics)


def acquire_verified_frames(
    sources: Mapping[str, StableFrameWaiter],
    *,
    reference_monotonic: float,
    timeout_s: float,
    stability_tolerance_px: float,
    stability_sample_count: int = 3,
    stability_sample_interval_s: float = 0.2,
    movement_context: CommandedMovementContext | None = None,
    cancelled: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, MotionAwareFrameResult]:
    """`acquire_verified_frame` over any number of named sources -- issue
    #30's own "The number of connected cameras is irrelevant. The policy
    shall operate per configured frame source." Each source is evaluated
    fully independently (its own stability window, its own outcome); a
    clear "wait for all / exclude optional / fail" policy across sources
    stays the caller's own decision, same as #27's `acquire_settled_frames`
    left composition policy to its own caller rather than hard-coding one.
    """
    return {
        key: acquire_verified_frame(
            waiter,
            reference_monotonic=reference_monotonic, timeout_s=timeout_s,
            stability_tolerance_px=stability_tolerance_px,
            stability_sample_count=stability_sample_count,
            stability_sample_interval_s=stability_sample_interval_s,
            movement_context=movement_context, cancelled=cancelled, sleep=sleep, now=now,
        )
        for key, waiter in sources.items()
    }
