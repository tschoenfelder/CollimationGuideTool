"""Operation lifecycle helpers (S6.3, #52 L01/L02/L04) -- small, not a controller.

Three pieces of knowledge that used to be re-implemented (and fixed in only some places):

- `OperationLifecycle`: ONE background operation at a time (idle -> running -> succeeded /
  failed / cancelled) whose terminal state and published outcome are GUARANTEED, whatever
  the work does. `MountTestMoveRunner` got this in 6b0a215 (#49); `AutofocusRunner`,
  `FineCollimationRunner` and `FovCalibrator` had no try/finally, so one exception left
  `is_busy` True until restart (audit L02). The outcome storage stays with each runner (each
  has its own outcome type and `take_latest()`); `publish` is called under `lock`, together
  with the transition, so a caller never sees "not busy" without the outcome.
- `completion_verdict`: the one-action-at-a-time confirmation of the device panels
  (focuser jog, filter slot change, park/unpark): seen-Busy then idle confirms; the
  safety-net timeout releases the controls EVEN WHILE the device still says Busy (3fc09ae --
  MountParkPanel still had the old `elif` shape, audit L01). The per-device timeouts stay
  with each panel (they legitimately differ: T01).
- `StatusPollGuard`: a periodic status poll (a QTimer slot) never raises into Qt (audit L04).
  A failure streak is logged once (and once more when it recovers); a programming error
  (`INTERNAL_ERRORS`, S6.2) is also handed to the #10 incident hook once per streak.

Quit semantics stay per device (audit L03, intentional): FocuserPanel aborts motion,
MountParkPanel turns tracking off (emergency stop), MountTestMovePanel does not abort its
in-flight runner itself -- MainWindow.closeEvent stops MountParkPanel before it, which is
what stops the mount. That ordering is load-bearing.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from enum import Enum

from astrotool_core.session import INTERNAL_ERRORS, LIFECYCLE_FAILURES, describe_failure

_log = logging.getLogger(__name__)


class OperationState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self not in (OperationState.IDLE, OperationState.RUNNING)


def _spawn_daemon(name: str) -> Callable[[Callable[[], None]], None]:
    def spawn(target: Callable[[], None]) -> None:
        threading.Thread(target=target, daemon=True, name=name).start()

    return spawn


class OperationLifecycle[OutcomeT]:
    """One operation at a time, with a guaranteed terminal state (see the module docstring).

    `cancel_event` is the Stop signal: pass it to every wait of the work
    (`Clock.sleep(s, cancel_event)`) so a Stop ends the wait in progress, not only the next
    step. It is cleared when the next operation is accepted."""

    def __init__(self, name: str) -> None:
        self._name = name
        #: Guards the state, and is held while `publish` stores the outcome.
        self.lock = threading.Lock()
        self._state = OperationState.IDLE
        self.cancel_event = threading.Event()

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> OperationState:
        with self.lock:
            return self._state

    @property
    def is_busy(self) -> bool:
        with self.lock:
            return self._state is OperationState.RUNNING

    def try_begin(self, on_begin: Callable[[], None] | None = None) -> bool:
        """Accept a new operation (False -- a no-op -- while one is running: "drop rather
        than queue"). `on_begin` runs under the lock, only for an accepted operation."""
        with self.lock:
            if self._state is OperationState.RUNNING:
                return False
            self._state = OperationState.RUNNING
            self.cancel_event.clear()
            if on_begin is not None:
                on_begin()
        return True

    def cancel(self) -> None:
        """Request cancellation of the running operation (any thread, returns at once; a
        no-op when idle -- the next accepted operation clears it)."""
        self.cancel_event.set()

    def is_cancel_requested(self) -> bool:
        return self.cancel_event.is_set()

    def run(
        self,
        work: Callable[[], OutcomeT],
        *,
        on_error: Callable[[Exception], OutcomeT],
        publish: Callable[[OutcomeT], None],
    ) -> None:
        """Run an accepted operation to its terminal state on the calling thread. Whatever
        `work` raises (any `Exception`) becomes `on_error(exc)`'s outcome. If `on_error`
        itself raises (e.g. an exception whose `str()` fails), it is asked once more with a
        plain RuntimeError naming both, so the UI still gets a FAILED outcome from
        `take_latest()`; only an `on_error` that fails for that too publishes nothing. Even
        then -- and when `publish` raises -- the operation never stays RUNNING."""
        failed = False
        try:
            outcome = work()
        except LIFECYCLE_FAILURES as exc:
            failed = True
            _log.exception("%s: the operation failed", self._name)
            try:
                outcome = on_error(exc)
            except Exception as build_exc:  # noqa: BLE001 -- see the docstring
                _log.exception("%s: building the failure outcome failed", self._name)
                fallback = RuntimeError(
                    f"{type(exc).__name__} (building its failure outcome raised "
                    f"{type(build_exc).__name__})"
                )
                try:
                    outcome = on_error(fallback)
                except Exception:  # noqa: BLE001 -- nothing may strand the operation RUNNING
                    _log.exception("%s: no failure outcome could be built", self._name)
                    with self.lock:
                        self._state = OperationState.FAILED
                    return
        with self.lock:
            if failed:
                self._state = OperationState.FAILED
            elif self.cancel_event.is_set():
                self._state = OperationState.CANCELLED
            else:
                self._state = OperationState.SUCCEEDED
            try:
                publish(outcome)
            except Exception:  # noqa: BLE001 -- see above
                _log.exception("%s: publishing the outcome failed", self._name)
                self._state = OperationState.FAILED

    def start(
        self,
        work: Callable[[], OutcomeT],
        *,
        on_error: Callable[[Exception], OutcomeT],
        publish: Callable[[OutcomeT], None],
        on_begin: Callable[[], None] | None = None,
        thread_name: str | None = None,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> bool:
        """`try_begin` + `run` on a background daemon thread (named `thread_name`, default
        the operation's name). False -- nothing started -- while one is running. `spawn`
        replaces the thread (tests run the work inline)."""
        if not self.try_begin(on_begin):
            return False
        launcher = spawn or _spawn_daemon(thread_name or self._name)
        launcher(lambda: self.run(work, on_error=on_error, publish=publish))
        return True


class CompletionVerdict(Enum):
    PENDING = "pending"
    #: The device was seen Busy and is idle again.
    CONFIRMED = "confirmed"
    #: The safety net: outstanding for longer than the timeout (whatever the device says).
    TIMED_OUT = "timed_out"


def completion_verdict(
    *,
    busy: bool | None,
    seen_busy: bool,
    issued_at: float | None,
    now: float,
    timeout_s: float,
) -> tuple[bool, CompletionVerdict]:
    """One poll of an outstanding device action: returns (`seen_busy` updated, verdict).

    `busy`: the device's reading this poll (None = unreadable: only the timeout decides).
    Idle before any Busy is NOT a confirmation (the driver's Busy lags the command). The
    timeout is checked whatever the reading -- a device stuck Busy forever must not keep the
    controls disabled (3fc09ae); `elapsed > timeout_s`, so exactly at the timeout the action
    is still awaited. A confirmation wins over a timeout seen in the same poll."""
    seen = seen_busy or busy is True
    if busy is False and seen:
        return seen, CompletionVerdict.CONFIRMED
    if issued_at is not None and now - issued_at > timeout_s:
        return seen, CompletionVerdict.TIMED_OUT
    return seen, CompletionVerdict.PENDING


class StatusPollGuard:
    """Runs one periodic status poll; never lets it raise into Qt (see the module docstring).

    `run` returns None when the poll succeeded, otherwise the operator-facing text (S6.2
    wording: the reason, or "Internal error -- ..." for a programming error)."""

    def __init__(self, device: str) -> None:
        self._device = device
        self._failing_text: str | None = None

    @property
    def failing(self) -> bool:
        return self._failing_text is not None

    def run(self, poll: Callable[[], object]) -> str | None:
        try:
            poll()
        except LIFECYCLE_FAILURES as exc:
            return self._failed(exc)
        if self._failing_text is not None:
            _log.info("%s: status read recovered", self._device)
            self._failing_text = None
        return None

    def _failed(self, exc: Exception) -> str:
        internal = isinstance(exc, INTERNAL_ERRORS)
        reason = f"{type(exc).__name__}: {exc}" if internal else describe_failure(exc)
        text = (
            f"Internal error — {reason} (capture diagnostics)"
            if internal
            else f"Status read failed — {reason}"
        )
        if self._failing_text is None:
            _log.log(
                logging.ERROR if internal else logging.WARNING,
                "%s: status read failed (%s): %s",
                self._device,
                type(exc).__name__,
                reason,
                exc_info=exc,
            )
            if internal:
                try:
                    sys.excepthook(type(exc), exc, exc.__traceback__)
                except Exception:  # noqa: BLE001 -- a failing hook must not break the poll
                    _log.exception("%s: the incident hook failed", self._device)
        self._failing_text = text
        return text
