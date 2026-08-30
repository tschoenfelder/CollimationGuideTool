"""MountTestMoveRunner — runs one mount test-move-and-measure pass on a
background thread, off the UI thread.

Mirrors `FovCalibrator`'s shape (submit()/take_latest()/is_busy, "run at
most one at a time", daemon background thread) for the same reason: the
pulse itself blocks for the requested duration
(`IndiMountPulseAdapter.pulse_axis` deliberately sleeps out the full
pulse — see that module's docstring) and detection runs on top of that,
so doing this inline from a button click would freeze the window for the
whole test.

Uses `astrotool_core.mount.axis_calibration.calibrate_axis_multi` so both
cameras are measured around the *same* single pulse, not one pulse per
camera — see that function's own docstring for why a naive two-calls
approach would be wrong here.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from astrotool_core.mount.axis_calibration import AxisResponse, calibrate_axis_multi
from astrotool_core.mount.port import AxisDirection, MountAxis, MountPort
from astrotool_core.target.detector import detect_sources

FrameGetter = Callable[[], np.ndarray | None]


@dataclass(frozen=True)
class MountTestMoveOutcome:
    """`responses` is empty and `error` set if the pulse itself was
    rejected or either camera couldn't be measured (before or after) —
    see module docstring: one shared pulse, so a measurement failure on
    either camera fails the whole attempt rather than reporting a partial
    result for just the other one."""

    responses: dict[str, AxisResponse]
    error: str | None = None


def _measure_brightest_source(get_frame: FrameGetter) -> tuple[float, float]:
    frame = get_frame()
    if frame is None:
        raise RuntimeError("no frame captured yet")
    result = detect_sources(frame)
    if not result.sources:
        raise RuntimeError("no point source detected in frame")
    brightest = max(result.sources, key=lambda source: source.peak)
    return (brightest.x, brightest.y)


class MountTestMoveRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._latest_outcome: MountTestMoveOutcome | None = None

    def submit(
        self,
        mount: MountPort,
        axis: MountAxis,
        direction: AxisDirection,
        pulse_ms: int,
        get_left_frame: FrameGetter,
        get_right_frame: FrameGetter,
    ) -> bool:
        """Start a test move in the background. Returns False (a no-op)
        if one is already running."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        threading.Thread(
            target=self._run,
            args=(mount, axis, direction, pulse_ms, get_left_frame, get_right_frame),
            daemon=True,
            name="mount-test-move",
        ).start()
        return True

    def _run(
        self,
        mount: MountPort,
        axis: MountAxis,
        direction: AxisDirection,
        pulse_ms: int,
        get_left_frame: FrameGetter,
        get_right_frame: FrameGetter,
    ) -> None:
        responses: dict[str, AxisResponse] = {}
        error: str | None = None
        try:
            responses = calibrate_axis_multi(
                mount,
                axis,
                direction,
                measures={
                    "left": lambda: _measure_brightest_source(get_left_frame),
                    "right": lambda: _measure_brightest_source(get_right_frame),
                },
                pulse_ms=pulse_ms,
            )
        except RuntimeError as exc:
            error = str(exc)
        with self._lock:
            self._latest_outcome = MountTestMoveOutcome(responses=responses, error=error)
            self._busy = False

    def take_latest(self) -> MountTestMoveOutcome | None:
        """Return and clear the latest completed outcome, if any — None
        means no test move has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy
