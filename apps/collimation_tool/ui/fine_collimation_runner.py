"""FineCollimationRunner — runs FineCollimationController.run() on a
background thread, off the UI thread. A real run collects several ROI
frames one camera exposure at a time before it can even begin stacking
and analysis -- the same UI-responsiveness class of concern already
solved four times over (FovCalibrator, MountTestMoveRunner,
AutofocusRunner, FrameAnalyzer); this mirrors their exact submit()/
take_latest()/is_busy shape, "run at most one at a time" semantics, and
daemon background thread -- the fifth instance of this established
convention.

S6.3 (#52 L02): the lifecycle (one run at a time, guaranteed terminal state) is
`OperationLifecycle`'s -- an exception escaping the run used to leave `is_busy` True for
good. A crashed run now ends as a failed outcome, reason `run_failed`, the exception in
`detail`.
"""

from __future__ import annotations

from dataclasses import dataclass

from collimation_tool.application.fine_collimation_controller import (
    FineCollimationController,
    FineCollimationOutcome,
)
from collimation_tool.application.operation_lifecycle import OperationLifecycle

#: `FineCollimationOutcome.reason` of a run that ended on an unexpected exception.
RUN_FAILED_REASON = "run_failed"


@dataclass(frozen=True)
class FineCollimationRunOutcome:
    outcome: FineCollimationOutcome


def _crashed(exc: Exception) -> FineCollimationRunOutcome:
    return FineCollimationRunOutcome(
        outcome=FineCollimationOutcome(
            status="failed", result=None, reason=RUN_FAILED_REASON,
            detail=f"{type(exc).__name__}: {exc}",
        )
    )


class FineCollimationRunner:
    def __init__(self) -> None:
        self._lifecycle: OperationLifecycle[FineCollimationRunOutcome] = OperationLifecycle(
            "fine-collimation-runner"
        )
        self._lock = self._lifecycle.lock
        self._latest_outcome: FineCollimationRunOutcome | None = None

    def submit(self, controller: FineCollimationController) -> bool:
        """Start a fine-collimation run in the background. Returns
        False (a no-op) if one is already running -- same "explicitly
        triggered, drop rather than queue" behavior as every prior
        runner in this project."""
        lifecycle = self._lifecycle
        return lifecycle.start(
            lambda: FineCollimationRunOutcome(
                outcome=controller.run(cancel_check=lifecycle.is_cancel_requested)
            ),
            on_error=_crashed,
            publish=self._publish,
        )

    def _publish(self, outcome: FineCollimationRunOutcome) -> None:
        self._latest_outcome = outcome  # called under self._lock

    def cancel(self) -> None:
        """Request cancellation of the in-flight run, if any -- a
        no-op if nothing is running."""
        self._lifecycle.cancel()

    def take_latest(self) -> FineCollimationRunOutcome | None:
        """Return and clear the latest completed outcome, if any --
        `None` means no run has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        return self._lifecycle.is_busy
