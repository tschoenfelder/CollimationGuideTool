"""FineCollimationRunner — runs FineCollimationController.run() on a
background thread, off the UI thread. A real run collects several ROI
frames one camera exposure at a time before it can even begin stacking
and analysis -- the same UI-responsiveness class of concern already
solved four times over (FovCalibrator, MountTestMoveRunner,
AutofocusRunner, FrameAnalyzer); this mirrors their exact submit()/
take_latest()/is_busy shape, "run at most one at a time" semantics, and
daemon background thread -- the fifth instance of this established
convention.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from collimation_tool.application.fine_collimation_controller import (
    FineCollimationController,
    FineCollimationOutcome,
)


@dataclass(frozen=True)
class FineCollimationRunOutcome:
    outcome: FineCollimationOutcome


class FineCollimationRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._latest_outcome: FineCollimationRunOutcome | None = None
        self._cancel_requested = False

    def submit(self, controller: FineCollimationController) -> bool:
        """Start a fine-collimation run in the background. Returns
        False (a no-op) if one is already running -- same "explicitly
        triggered, drop rather than queue" behavior as every prior
        runner in this project."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._cancel_requested = False
        threading.Thread(
            target=self._run, args=(controller,), daemon=True, name="fine-collimation-runner",
        ).start()
        return True

    def cancel(self) -> None:
        """Request cancellation of the in-flight run, if any -- a
        no-op if nothing is running."""
        with self._lock:
            self._cancel_requested = True

    def _is_cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    def _run(self, controller: FineCollimationController) -> None:
        outcome = controller.run(cancel_check=self._is_cancel_requested)
        with self._lock:
            self._latest_outcome = FineCollimationRunOutcome(outcome=outcome)
            self._busy = False

    def take_latest(self) -> FineCollimationRunOutcome | None:
        """Return and clear the latest completed outcome, if any --
        `None` means no run has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy
