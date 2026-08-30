"""MountTestMoveRunner — runs one mount unpark/pulse/re-park sequence on
a background thread, off the UI thread.

Mirrors `FovCalibrator`'s shape (submit()/take_latest()/is_busy, "run at
most one at a time", daemon background thread) for the same reason: the
pulse itself blocks for the requested duration
(`IndiMountPulseAdapter.pulse_axis` deliberately sleeps out the full
pulse — see that module's docstring), so doing this inline from a button
click would freeze the window for the whole test.

Deliberately does *not* touch any camera/frame accessor, unlike an
earlier version of this module — a real crash was traced to exactly
that: calling `CameraPanel.latest_mono_frame()` from this background
thread while the same panel's own Qt poll timer was concurrently
delivering new frames on the main thread (both touch the panel's
captured-frame state; `FovCalibrator`'s own established pattern never
has this problem because its background thread only ever processes
plain numpy arrays already captured on the main thread before submit()).
So here: `MountTestMovePanel` captures the "before" frames itself on the
main thread, submits only the mount sequence to this runner, and — once
`take_latest()` reports the pulse finished — captures the "after" frames
and computes the response itself, also on the main thread (fast enough,
unlike FOV registration's search, not to need its own thread). This
runner's only job is the unpark/pulse/re-park timing.

Unparks first, pulses, then re-parks — a real-hardware check (see
incident notes on `IndiMountPulseAdapter`) found OnStep's driver refuses
`TELESCOPE_MOTION_NS`/`_WE` while parked ("Please unpark the mount before
issuing any motion/sync commands"). That refusal is a deliberate safety
interlock, not a defect — parked is supposed to mean "don't move" — so
this runner works *with* it rather than around it: it still reports the
switch command accepted at the INDI level even though nothing moved, so
a caller can't tell from the ack alone, which is why this runner does the
unpark/re-park itself instead (reusing `IndiMountParkAdapter.unpark()`'s
already-durable TRACK_OFF), so from the button's perspective the mount
still starts and ends parked — "when parked" describes the resting state
around the test, not a precondition the pulse itself can honor, and the
interlock stays intact and respected throughout. `park()` always runs in
a `finally`, even if the pulse itself failed, so a mid-run error can't
strand the mount unparked.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.mount.port import AxisDirection, MountAxis, MountPort

_UNPARK_TIMEOUT_S = 5.0
_REPARK_TIMEOUT_S = 5.0
_PARK_POLL_INTERVAL_S = 0.1


@dataclass(frozen=True)
class MountPulseOutcome:
    """`pulsed` is False and `error` set if the mount never confirmed
    unparked or the pulse itself was rejected — see module docstring for
    why re-parking is always attempted regardless."""

    pulsed: bool
    error: str | None = None


def _wait_for_parked(mount_park: MountParkPort, *, want_parked: bool, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if mount_park.status().parked == want_parked:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PARK_POLL_INTERVAL_S)


class MountTestMoveRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._latest_outcome: MountPulseOutcome | None = None

    def submit(
        self,
        mount_park: MountParkPort,
        mount: MountPort,
        axis: MountAxis,
        direction: AxisDirection,
        pulse_ms: int,
    ) -> bool:
        """Start an unpark/pulse/re-park sequence in the background.
        Returns False (a no-op) if one is already running."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        threading.Thread(
            target=self._run,
            args=(mount_park, mount, axis, direction, pulse_ms),
            daemon=True,
            name="mount-test-move",
        ).start()
        return True

    def _run(
        self,
        mount_park: MountParkPort,
        mount: MountPort,
        axis: MountAxis,
        direction: AxisDirection,
        pulse_ms: int,
    ) -> None:
        pulsed = False
        error: str | None = None
        mount_park.unpark()
        try:
            if not _wait_for_parked(mount_park, want_parked=False, timeout_s=_UNPARK_TIMEOUT_S):
                error = "mount did not confirm unparked in time -- aborting test move"
            else:
                result = mount.pulse_axis(axis, direction, pulse_ms)
                if not result.accepted:
                    error = (
                        f"pulse rejected for {axis.name} {direction.name}: {result.message}"
                    )
                else:
                    pulsed = True
        finally:
            # Always try to leave the mount parked again, even if the
            # pulse failed above -- see module docstring.
            mount_park.park()
            if not _wait_for_parked(mount_park, want_parked=True, timeout_s=_REPARK_TIMEOUT_S):
                error = error or "mount did not confirm re-parked in time"

        with self._lock:
            self._latest_outcome = MountPulseOutcome(pulsed=pulsed, error=error)
            self._busy = False

    def take_latest(self) -> MountPulseOutcome | None:
        """Return and clear the latest completed outcome, if any — None
        means no test move has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy
