"""FovCalibrator — runs fov_registration.register_main_frame_in_guide_frame
on a background thread, off the UI thread.

Even with fairly coarse default search parameters, a full rotation+scale
search over real camera-sized frames can take a few real seconds (see
`fov_registration`'s module docstring) — long enough to freeze the
window's event loop if run inline from a button click, exactly the
UI-responsiveness class of bug this project already had to fix once for
the per-frame analysis pipeline (see `FrameAnalyzer`, which this
mirrors: same submit()/take_latest()/is_busy shape, "run at most one at
a time" semantics, and daemon background thread).
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
        threading.Thread(
            target=self._run,
            args=(main_mono, guide_mono, approx_scale),
            daemon=True,
            name="fov-calibrator",
        ).start()
        return True

    def _run(self, main_mono: np.ndarray, guide_mono: np.ndarray, approx_scale: float) -> None:
        result: FovRegistrationResult | None = None
        try:
            result = register_main_frame_in_guide_frame(
                main_mono, guide_mono, approx_scale=approx_scale
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

    def take_latest(self) -> FovCalibrationOutcome | None:
        """Return and clear the latest completed outcome, if any —
        `None` means no calibration has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy
