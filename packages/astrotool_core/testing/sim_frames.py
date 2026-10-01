"""Deterministic frame delivery with explicit timestamps (issue #51, for
#30/#43/#49).

`FrameTimeline` is a frame source on a clock: every frame has an explicit
exposure start, exposure length and delivery time (`captured_at`, the end of
the exposure, matching `StreamController`'s "after capture" stamp). Its
`next_frame(remaining_s)` is exactly the `NextFrame` contract
`acquire_stable_frame` consumes: frames already delivered but not yet read
come back at once, oldest first (a stale/queued backlog), otherwise the clock
advances to the next delivery -- or by `remaining_s` and None when nothing is
due in time. So stale frames, exposures overlapping a commanded motion, and
long exposures are all reproducible to the microsecond without a camera,
thread or real sleep.

Frames come from either a periodic exposure schedule (`schedule_stream`) or
explicit `FrameSpec`s; pixels from a `render(start, end)` callback, a fixed
list (e.g. FITS frames from `replay_dataset.load_frames` via
`frames_from_replay`), or a blank default. `MotionWindow`s mark when the mount
moved, so a test can state which delivered frames integrated motion
(`overlaps_motion`).

`CameraFrameSource` turns any `CameraPort` (e.g. the production
`TouptekCameraAdapter` over `sim_touptek`) into the same `NextFrame` shape,
stamping each capture with the clock after `capture()` returns.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    acquire_stable_frame,
)
from astrotool_core.camera.port import CameraPort
from astrotool_core.frames.frame import Frame
from astrotool_core.timing import Clock

#: (exposure start, exposure end) -> pixels.
Render = Callable[[float, float], np.ndarray]


@dataclass(frozen=True)
class MotionWindow:
    """The mount moved from `start` to `end` (clock time)."""

    start: float
    end: float


@dataclass(frozen=True)
class FrameSpec:
    exposure_start: float
    exposure_s: float

    @property
    def captured_at(self) -> float:
        return self.exposure_start + self.exposure_s


@dataclass
class TimelineFrame:
    spec: FrameSpec
    frame: DeliveredFrame
    overlaps_motion: bool


@dataclass
class FrameTimeline:
    clock: Clock
    render: Render | None = None
    motions: list[MotionWindow] = field(default_factory=list)
    shape: tuple[int, int] = (32, 32)
    _pending: list[FrameSpec] = field(default_factory=list)
    _pixels: list[np.ndarray] = field(default_factory=list)
    #: Every frame handed out, in order.
    delivered: list[TimelineFrame] = field(default_factory=list)

    # -- building the schedule ------------------------------------------------
    def add(self, *specs: FrameSpec) -> FrameTimeline:
        self._pending.extend(specs)
        self._pending.sort(key=lambda s: s.captured_at)
        return self

    def schedule_stream(
        self, *, first_start: float, exposure_s: float, cadence_s: float, count: int
    ) -> FrameTimeline:
        """`count` back-to-back exposures: frame k starts at
        `first_start + k * cadence_s` and is delivered `exposure_s` later."""
        return self.add(*(FrameSpec(first_start + k * cadence_s, exposure_s) for k in range(count)))

    def use_pixels(self, frames: Sequence[np.ndarray]) -> FrameTimeline:
        """Fixed pixel data, one per delivered frame, in order (cycled)."""
        self._pixels = [np.asarray(f, dtype=np.float32) for f in frames]
        return self

    def add_motion(self, start: float, end: float) -> FrameTimeline:
        self.motions.append(MotionWindow(start, end))
        return self

    # -- the NextFrame contract ---------------------------------------------------
    def overlaps_motion(self, spec: FrameSpec) -> bool:
        return any(spec.exposure_start < m.end and spec.captured_at > m.start for m in self.motions)

    def _pixels_for(self, spec: FrameSpec) -> np.ndarray:
        if self.render is not None:
            return np.asarray(self.render(spec.exposure_start, spec.captured_at), np.float32)
        if self._pixels:
            return self._pixels[len(self.delivered) % len(self._pixels)]
        return np.zeros(self.shape, dtype=np.float32)

    def next_frame(self, remaining_s: float) -> DeliveredFrame | None:
        now = self.clock.monotonic()
        if not self._pending or self._pending[0].captured_at > now + remaining_s:
            self.clock.sleep(max(0.0, remaining_s))
            return None
        spec = self._pending.pop(0)
        if spec.captured_at > now:
            self.clock.sleep(spec.captured_at - now)
        frame = DeliveredFrame(self._pixels_for(spec), spec.captured_at, spec.exposure_s)
        self.delivered.append(TimelineFrame(spec, frame, self.overlaps_motion(spec)))
        return frame

    def waiter(self, *, available: bool = True) -> Callable[[float, float], FrameAcquisitionResult]:
        """A `StableFrameWaiter` (the `CameraPanel.wait_for_frame_after`
        shape) over this timeline, deciding with the production
        `acquire_stable_frame`."""

        def wait(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            return acquire_stable_frame(
                self.next_frame,
                is_available=lambda: available,
                reference_monotonic=reference,
                timeout_s=timeout_s,
                clock=self.clock,
            )

        return wait


def frames_from_replay(frames: Sequence[Frame]) -> list[np.ndarray]:
    """Pixel arrays of replayed FITS frames (`replay_dataset.load_frames`)."""
    return [np.asarray(frame.pixels, dtype=np.float32) for frame in frames]


@dataclass
class CameraFrameSource:
    """`NextFrame` over a `CameraPort`: captures on demand, stamped by `clock`."""

    camera: CameraPort
    clock: Clock
    exposure_s: float

    def next_frame(self, _remaining_s: float) -> DeliveredFrame | None:
        frame = self.camera.capture(self.exposure_s)
        return DeliveredFrame(
            np.asarray(frame.pixels, dtype=np.float32),
            self.clock.monotonic(),
            frame.exposure_seconds,
        )

    def waiter(self) -> Callable[[float, float], FrameAcquisitionResult]:
        def wait(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            return acquire_stable_frame(
                self.next_frame,
                is_available=lambda: True,
                reference_monotonic=reference,
                timeout_s=timeout_s,
                clock=self.clock,
            )

        return wait
