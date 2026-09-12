"""AutofocusRunner — runs AutofocusController.run() on a background
thread, off the UI thread. A real autofocus run drives a real bounded
focuser search plus multiple camera exposures per sample, easily taking
seconds to tens of seconds — the same UI-responsiveness class of concern
already solved for FOV calibration (`FovCalibrator`) and mount calibration
(`MountTestMoveRunner`); this mirrors their exact submit()/take_latest()/
is_busy shape, "run at most one at a time" semantics, and daemon
background thread -- the third instance of this established convention.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from collimation_tool.application.autofocus_controller import (
    AutofocusController,
    AutofocusMode,
    AutofocusResult,
)


@dataclass(frozen=True)
class AutofocusOutcome:
    result: AutofocusResult


class AutofocusRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._latest_outcome: AutofocusOutcome | None = None
        self._cancel_requested = False

    def submit(self, controller: AutofocusController, mode: AutofocusMode) -> bool:
        """Start an autofocus run in the background. Returns False (a
        no-op) if one is already running -- same "explicitly triggered,
        drop rather than queue" behavior as FovCalibrator.submit()."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel_requested = False
        threading.Thread(
            target=self._run, args=(controller, mode), daemon=True, name="autofocus-runner",
        ).start()
        return True

    def cancel(self) -> None:
        """Request cancellation of the in-flight run, if any -- a no-op
        if nothing is running. The run itself decides when it's safe to
        stop (see AutofocusController.run's own cancel_check contract)."""
        with self._lock:
            self._cancel_requested = True

    def _is_cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def _run(self, controller: AutofocusController, mode: AutofocusMode) -> None:
        result = controller.run(mode, cancel_check=self._is_cancel_requested)
        with self._lock:
            self._latest_outcome = AutofocusOutcome(result=result)
            self._busy = False

    def take_latest(self) -> AutofocusOutcome | None:
        """Return and clear the latest completed outcome, if any --
        `None` means no run has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy
