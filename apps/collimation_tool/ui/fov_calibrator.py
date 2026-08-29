"""FovCalibrator — runs fov_registration.register_main_frame_in_guide_frame
on a background thread, off the UI thread.

Even with fairly coarse default search parameters, a full rotation+scale
search over real camera-sized frames takes on the order of two real
minutes (see `fov_registration`'s module docstring — measured on a real
rig) — long enough to freeze the window's event loop if run inline from
a button click, exactly the UI-responsiveness class of bug this project
already had to fix once for the per-frame analysis pipeline (see
`FrameAnalyzer`, which this mirrors: same submit()/take_latest()/is_busy
shape, "run at most one at a time" semantics, and daemon background
thread).

Also forwards fov_registration's progress_callback so a caller can show
something better than a static "working" message for those two minutes
— see `latest_progress` and the real bug this was added for: a user
report that "Calibration started but working without any status on
progress" was, on inspection, expected behavior (the search was
genuinely still running) that looked indistinguishable from a hang.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from collimation_tool.ui.fov_registration import (
    FovRegistrationResult,
    register_main_frame_in_guide_frame,
)


@dataclass(frozen=True)
class FovCalibrationOutcome:
    """Wraps a completed calibration's result so `take_latest()` can tell
    "nothing has finished since the last check" (returns `None`) apart
    from "finished, but found no confident match" (returns an outcome
    whose own `result` is `None`) — `register_main_frame_in_guide_frame`
    itself already uses `None` for the latter, which would otherwise be
    ambiguous with FrameAnalyzer's polling idiom.
    """

    result: FovRegistrationResult | None


class FovCalibrator:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._latest_outcome: FovCalibrationOutcome | None = None
        #: (completed, total) candidates scored so far — see
        #: latest_progress(). None while idle or before the first
        #: candidate of a run has been scored.
        self._progress: tuple[int, int] | None = None

    def submit(self, main_mono: np.ndarray, guide_mono: np.ndarray, *, approx_scale: float) -> bool:
        """Start a calibration in the background. Returns False (a no-op)
        if one is already running — matches FrameAnalyzer's
        drop-rather-than-queue behavior, appropriate here too since this
        is explicitly triggered (see the "Calibrate FOV" button), not a
        per-frame submission that will naturally get a fresher retry."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._progress = None
        threading.Thread(
            target=self._run,
            args=(main_mono, guide_mono, approx_scale),
            daemon=True,
            name="fov-calibrator",
        ).start()
        return True

    def _on_progress(self, completed: int, total: int) -> None:
        with self._lock:
            self._progress = (completed, total)

    def _run(self, main_mono: np.ndarray, guide_mono: np.ndarray, approx_scale: float) -> None:
        result: FovRegistrationResult | None = None
        try:
            result = register_main_frame_in_guide_frame(
                main_mono,
                guide_mono,
                approx_scale=approx_scale,
                progress_callback=self._on_progress,
            )
        except ValueError:
            # Bad input (e.g. a template that can't fit at any candidate
            # scale) — treat exactly like "no confident match", not a
            # crash of this background thread.
            result = None
        finally:
            with self._lock:
                self._latest_outcome = FovCalibrationOutcome(result=result)
                self._busy = False
                self._progress = None  # nothing left to report once finished

    def take_latest(self) -> FovCalibrationOutcome | None:
        """Return and clear the latest completed outcome, if any —
        `None` means no calibration has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    def latest_progress(self) -> tuple[int, int] | None:
        """Current (completed, total) candidate count for an in-flight
        calibration, or None if idle or nothing has been scored yet.

        Unlike take_latest(), this does not clear on read — a caller
        polling every tick while a calibration runs (see MainWindow) is
        meant to see the same value repeatedly until it changes.
        """
        with self._lock:
            return self._progress

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy
