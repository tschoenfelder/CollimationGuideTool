"""AutofocusRunner — runs AutofocusController.run() on a background
thread, off the UI thread. A real autofocus run drives a real bounded
focuser search plus multiple camera exposures per sample, easily taking
seconds to tens of seconds — the same UI-responsiveness class of concern
already solved for FOV calibration (`FovCalibrator`) and mount calibration
(`MountTestMoveRunner`); this mirrors their exact submit()/take_latest()/
is_busy shape, "run at most one at a time" semantics, and daemon
background thread -- the third instance of this established convention.

S6.3 (#52 L02): the lifecycle itself (one run at a time, guaranteed terminal state, Stop
event) is `OperationLifecycle`'s -- an exception escaping the run used to leave `is_busy`
True for good. A crashed run now ends as an `AutofocusStatus.ERROR` result whose
`failure_reason` names the exception; the runner's stop event also reaches the controller's
own waits (the exposure settle), not only its step checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from collimation_tool.application.autofocus_controller import (
    AutofocusController,
    AutofocusMode,
    AutofocusResult,
)
from collimation_tool.application.autofocus_search import AutofocusStatus
from collimation_tool.application.operation_lifecycle import OperationLifecycle

#: `AutofocusResult.start_position` of a run that crashed: the runner never learnt it.
UNKNOWN_START_POSITION = -1


@dataclass(frozen=True)
class AutofocusOutcome:
    result: AutofocusResult


def _crashed(mode: AutofocusMode, exc: Exception) -> AutofocusOutcome:
    return AutofocusOutcome(
        result=AutofocusResult(
            status=AutofocusStatus.ERROR,
            mode=mode,
            start_position=UNKNOWN_START_POSITION,
            best_position=None,
            search_min=None,
            search_max=None,
            failure_reason=f"{type(exc).__name__}: {exc}",
        )
    )


class AutofocusRunner:
    def __init__(self) -> None:
        self._lifecycle: OperationLifecycle[AutofocusOutcome] = OperationLifecycle(
            "autofocus-runner"
        )
        self._lock = self._lifecycle.lock
        self._latest_outcome: AutofocusOutcome | None = None

    def submit(self, controller: AutofocusController, mode: AutofocusMode) -> bool:
        """Start an autofocus run in the background. Returns False (a no-op) if one is
        already running -- same "explicitly triggered, drop rather than queue" behavior as
        FovCalibrator.submit()."""
        lifecycle = self._lifecycle
        return lifecycle.start(
            lambda: AutofocusOutcome(
                result=controller.run(
                    mode,
                    cancel_check=lifecycle.is_cancel_requested,
                    cancel_event=lifecycle.cancel_event,
                )
            ),
            on_error=lambda exc: _crashed(mode, exc),
            publish=self._publish,
        )

    def _publish(self, outcome: AutofocusOutcome) -> None:
        self._latest_outcome = outcome  # called under self._lock

    def cancel(self) -> None:
        """Request cancellation of the in-flight run, if any -- a no-op if nothing is
        running. The run decides when it's safe to stop (AutofocusController.run's
        cancel_check contract); a wait in progress ends at once (cancel_event)."""
        self._lifecycle.cancel()

    def take_latest(self) -> AutofocusOutcome | None:
        """Return and clear the latest completed outcome, if any --
        `None` means no run has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        return self._lifecycle.is_busy
