"""Stable post-motion frame acquisition — decides whether a captured frame
is trustworthy to measure a mount-motion displacement from, independent of
how many frame sources are involved or how a displacement is later
measured.

Issue #27: real-hardware calibration work kept mixing two linearly
independent concerns inside `MountTestMovePanel` — (A) whether a captured
frame is even valid to measure from (did its own exposure start only after
the mount finished moving? did the configured settling policy elapse? did
anything arrive before timeout? is the source available at all?), and (B)
the actual pixel-level displacement measurement
(`astrotool_core.target.translation_offset.measure_translation_offset`,
untouched by this module). Mixing them made a failure's real cause — no
camera, no frame in time, or a frame whose own exposure genuinely
overlapped the move — indistinguishable in the field ("calibration
failed" was the only signal a diagnostic bundle carried). This module owns
(A) alone: it knows nothing about mount motion, INDI, OnStep, the number
of cameras, or Qt/UI state — only frame timestamps, exposure durations,
and a caller-supplied clock/sleep, so it's directly testable with plain
callables (see `tests/core/acquisition/test_stable_frame_acquisition.py`).

Two entry points, matching the two places this replaces ad hoc logic:

- `acquire_stable_frame` — one frame source's own answer to "is there a
  frame I can trust was captured only after a given reference time?".
  `apps/collimation_tool/ui/camera_panel.py`'s `CameraPanel.wait_for_frame_after`
  is a thin wrapper around this.
- `acquire_settled_frames` — composes any number of already-built
  per-source waiters (each shaped like `acquire_stable_frame`'s own
  return contract) with a two-stage settling policy, camera-count-
  independent by construction (a plain `Mapping[str, ...]`, not a
  left/right pair). `apps/collimation_tool/ui/mount_test_move_panel.py`'s
  `MountTestMovePanel._capture_both` composes over this for its own two
  named cameras today, but nothing here assumes exactly two.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum

import numpy as np


@dataclass(frozen=True)
class DeliveredFrame:
    """One frame as delivered by a frame source, with just enough timing
    metadata for a stable-frame decision — a plain mono pixel array, not
    any particular camera/frame type, so this module has no dependency on
    `astrotool_core.camera` or `astrotool_core.frames`."""

    pixels: np.ndarray
    captured_at_monotonic: float
    exposure_seconds: float


class FrameAcquisitionStatus(Enum):
    """Why a stable-frame acquisition did or didn't produce a usable frame
    — see this module's own docstring and issue #27's "Failure semantics"
    section, whose suggested names these match directly."""

    #: A usable frame was found — `FrameAcquisitionResult.frame` is set.
    OK = "ok"
    #: Nothing arrived before `timeout_s`, and every frame that did arrive
    #: (if any) already satisfied the reference — plain "took too long".
    TIMEOUT = "timeout"
    #: Every frame delivered before the deadline had its own exposure
    #: start before `reference_monotonic` — a real report, diagnostic
    #: c7dc2c3d ("still using frames during movement"): a frame delivered
    #: comfortably after a pulse/settle finished can still integrate real
    #: light captured *during* it, if the exposure is long enough.
    EXPOSURE_OVERLAPPED_MOTION = "exposure_overlapped_motion"
    #: The source isn't currently producing frames at all (not streaming,
    #: not connected) — checked once, up front, distinct from a live
    #: source that simply never delivered a fresh-enough frame in time.
    CAMERA_UNAVAILABLE = "camera_unavailable"
    #: `acquire_settled_frames` only — every source caught up at stage 1,
    #: but this one didn't produce another frame after the extra settle
    #: wait at stage 2.
    SETTLE_NOT_REACHED = "settle_not_reached"
    #: The caller's own `cancelled()` callback fired mid-wait.
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class FrameAcquisitionResult:
    status: FrameAcquisitionStatus
    frame: DeliveredFrame | None = None

    @property
    def ok(self) -> bool:
        return self.status is FrameAcquisitionStatus.OK


#: `next_frame(remaining_timeout_s)` must return the next frame a source
#: has produced since the *previous* call (never replaying one already
#: returned), or None once `remaining_timeout_s` elapses with nothing new
#: — e.g. a thin wrapper around a mailbox/queue `wait`.
NextFrame = Callable[[float], "DeliveredFrame | None"]

#: `acquire_stable_frame`'s own call signature, reused by
#: `acquire_settled_frames` to compose several already-built waiters (one
#: per named source) together.
StableFrameWaiter = Callable[[float, float], FrameAcquisitionResult]


def acquire_stable_frame(
    next_frame: NextFrame,
    *,
    is_available: Callable[[], bool],
    reference_monotonic: float,
    timeout_s: float,
    cancelled: Callable[[], bool] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> FrameAcquisitionResult:
    """One frame source's own answer to "is there a frame I can trust was
    captured only after `reference_monotonic`?".

    Blocks (via repeated calls to `next_frame`) up to `timeout_s`, only
    ever returning `OK` for a frame whose own exposure genuinely started
    at or after `reference_monotonic` — not merely delivered after it, see
    `FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION`.

    `is_available()` is checked once, up front, before any `next_frame`
    call — see `FrameAcquisitionStatus.CAMERA_UNAVAILABLE`.

    `cancelled()`, when given, is checked before every `next_frame` call —
    lets a caller interrupt an in-progress wait (e.g. a "Stop" button)
    without having to fake a timeout. Defaults to never-cancelled.
    """
    is_cancelled = cancelled or (lambda: False)
    if not is_available():
        return FrameAcquisitionResult(FrameAcquisitionStatus.CAMERA_UNAVAILABLE)
    deadline = now() + timeout_s
    saw_overlapping_exposure = False
    while True:
        if is_cancelled():
            return FrameAcquisitionResult(FrameAcquisitionStatus.CANCELLED)
        remaining = deadline - now()
        if remaining <= 0:
            break
        frame = next_frame(remaining)
        if frame is None:
            break
        exposure_start = frame.captured_at_monotonic - frame.exposure_seconds
        if exposure_start >= reference_monotonic:
            return FrameAcquisitionResult(FrameAcquisitionStatus.OK, frame)
        saw_overlapping_exposure = True
    status = (
        FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION
        if saw_overlapping_exposure
        else FrameAcquisitionStatus.TIMEOUT
    )
    return FrameAcquisitionResult(status)


def acquire_settled_frames(
    sources: Mapping[str, StableFrameWaiter],
    *,
    reference_monotonic: float,
    timeout_s: float,
    settle_ms: int,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict[str, FrameAcquisitionResult]:
    """Composes any number of already-built per-source stable-frame
    waiters (each shaped like `acquire_stable_frame`'s own
    `(reference_monotonic, timeout_s) -> FrameAcquisitionResult` contract
    — e.g. `CameraPanel.wait_for_frame_after` bound per camera) with a
    two-stage settling policy, independent of how many sources there are
    (issue #27: "the number of connected cameras is irrelevant to this
    architectural boundary").

    Real report ("still 2-3 frames are shown showing movement" after a
    pulse): a single frame delivered past the pulse-completion reference
    isn't strong enough evidence the mount has actually finished
    mechanically settling — residual vibration/backlash damping out can
    outlast both `settle_ms` and that very first fresh-delivered frame. So
    this first confirms every source has caught up past
    `reference_monotonic` (stage 1), then grants `settle_ms` again, and
    only *then* takes the frame actually returned (stage 2) — from that
    later point, not the first barely-fresh one.

    Stage 2 only runs once *every* source has succeeded at stage 1 — if
    any source is missing a frame at all yet, there's nothing to settle
    from, and the stage-1 results (each with its own real
    `FrameAcquisitionStatus`) are returned as-is. A stage-2 failure is
    reported as `SETTLE_NOT_REACHED` regardless of that source's own
    stage-2 status — a source that did catch up once but couldn't produce
    another frame after the extra settle wait, distinct from never having
    caught up at all.
    """
    stage1 = {key: waiter(reference_monotonic, timeout_s) for key, waiter in sources.items()}
    if not all(result.ok for result in stage1.values()):
        return stage1
    sleep(settle_ms / 1000.0)
    settled_at = now()
    stage2 = {key: waiter(settled_at, timeout_s) for key, waiter in sources.items()}
    return {
        key: (
            result
            if result.ok
            else FrameAcquisitionResult(FrameAcquisitionStatus.SETTLE_NOT_REACHED)
        )
        for key, result in stage2.items()
    }
