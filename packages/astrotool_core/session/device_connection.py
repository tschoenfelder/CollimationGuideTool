"""Device connection lifecycle -- the ONE owner of how a device connection is opened and
released and of how its failures are reported (issue #52, audit K01-K03 in
`docs/quality/duplication-audit.md`).

Before this module, every panel (Focuser, MountPark, FilterWheel, MountTestMove, Camera) and
the GuideTool wrote its own "connect failed" handling, and they disagreed: which exceptions
were caught (GuideTool: only `ConnectionError` -- the 7bb7b68 bug class), whether the reason
was logged and kept for diagnostics (FilterWheelPanel: neither -- the ca5bb92 bug class),
whether a camera stream start guarded `connect()` at all, whether the previous camera was
released on a switch (GuideTool: never -- the #45 ERROR_BUSY class), and whether a raising
stop/disconnect skipped the rest of the cleanup (every panel's `stop()`, and
`MainWindow.closeEvent` across panels).

What this module owns:

- the failure set (`LIFECYCLE_FAILURES`), split into device failures and INTERNAL errors
  (`INTERNAL_ERRORS`), and the normalized failure (`ConnectionFailure`, `describe_failure`)
  -- device failures include what OnStepAdapter's `connect()` really raises
  (ConnectionError/TimeoutError/OSError from the INDI socket, RuntimeError/ValueError from its
  own checks, KeyError from a property reply missing a field, indi_client.py `connect`);
- the ONE log record per failure (`run_phase`, with its traceback), and for an internal error
  the hand-off to the app's #10 incident capture (`sys.excepthook`);
- the phase order and the guaranteed cleanup: `open_device` is all-or-nothing (a failure in
  any opening phase releases the device again, so nothing stays half-open and a retry starts
  clean); `close_device` and `stop_each` never raise and never skip a later step;
- releasing the previous device before opening a new one, with a restore on failure
  (`switch_device`, #45: the vendor SDK allows one open handle per device).

Phases (`ConnectionPhase`): CONNECT the device's own `connect()`; START the post-connect
setup the caller needs before the device counts as connected (e.g. the #44 tracking
enforcement, a camera stream start); WAIT the first status read that shows the device
answers; CANCEL stopping motion still in flight before the release (focuser abort, mount
tracking off); DISCONNECT the release itself; SHUTDOWN one component's own stop at app exit.

It is deliberately small: no Qt, no threads, no waits (every wait stays inside the adapters
and their injected clocks); panels keep their own widgets and bind to the result. Busy /
confirmation-timeout policy is S6.3 (`OperationLifecycle`), not this.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

_log = logging.getLogger(__name__)


class ConnectableDevice(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...


class ConnectionPhase(Enum):
    CONNECT = "connect"
    START = "start"
    WAIT = "wait"
    CANCEL = "cancel"
    DISCONNECT = "disconnect"
    SHUTDOWN = "shutdown"


#: Every failure a lifecycle phase turns into a `ConnectionFailure` instead of letting it escape
#: the click/quit handler: any `Exception`. A narrower list of "device" types is how the 7bb7b68
#: (TimeoutError) and GuideTool (non-ConnectionError) crashes shipped -- vendor SDK errors
#: (ToupTek `HRESULTException`), INDI socket errors and OnStepAdapter's KeyError are not
#: ConnectionErrors. `BaseException`s that are not `Exception`s (KeyboardInterrupt,
#: SystemExit) are never caught.
LIFECYCLE_FAILURES: tuple[type[Exception], ...] = (Exception,)

#: The caught exceptions that mean a bug in this code (or a broken install), not a device that
#: did not answer. They are still contained (the panel stays usable and retryable) but are
#: reported as INTERNAL: logged at ERROR, worded "Internal error", and handed to
#: `sys.excepthook` -- the apps install their #10 diagnostic-incident capture there, so these
#: still produce an incident bundle, as they did when they escaped the handler. Everything else
#: (KeyError, OSError incl. ConnectionError/TimeoutError, RuntimeError, ValueError, vendor SDK
#: errors) is a device failure. UnboundLocalError is a NameError, ModuleNotFoundError an
#: ImportError.
INTERNAL_ERRORS: tuple[type[Exception], ...] = (
    AttributeError,
    TypeError,
    NameError,
    AssertionError,
    ImportError,
)

_STATUS_PREFIX = {
    ConnectionPhase.CONNECT: "Connect failed",
    ConnectionPhase.START: "Connect failed (setup)",
    ConnectionPhase.WAIT: "Connect failed (no status)",
    ConnectionPhase.CANCEL: "Stop failed",
    ConnectionPhase.DISCONNECT: "Disconnect failed",
    ConnectionPhase.SHUTDOWN: "Shutdown failed",
}


def describe_failure(exc: BaseException) -> str:
    """The operator-facing reason. `str(exc)` as is, except where that alone says nothing: a
    `KeyError` stringifies to just the quoted key (OnStepAdapter's missing-field case would
    read `'UTC'`), and some SDK errors have no message at all -- those carry the type name."""
    text = str(exc)
    if not text:
        return type(exc).__name__
    if isinstance(exc, KeyError):
        return f"{type(exc).__name__}: {text}"
    return text


@dataclass(frozen=True)
class ConnectionFailure:
    device: str
    phase: ConnectionPhase
    error_type: str
    message: str
    #: A programming error (`INTERNAL_ERRORS`), not a device failure.
    internal: bool = False

    @property
    def status_text(self) -> str:
        """The one status-line wording, e.g. "Connect failed — <reason>"."""
        if self.internal:
            return f"Internal error — {self.message} (capture diagnostics)"
        return f"{_STATUS_PREFIX[self.phase]} — {self.message}"

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "device": self.device,
            "phase": self.phase.value,
            "error_type": self.error_type,
            "message": self.message,
            "internal": self.internal,
        }


def run_phase(
    device: str, phase: ConnectionPhase, step: Callable[[], object]
) -> ConnectionFailure | None:
    """Run one lifecycle step. A failure is normalized, logged exactly once -- one record,
    "<device>: <phase> failed (<ExceptionType>): <reason>", with its traceback; WARNING for a
    device failure, ERROR for an internal one -- and returned, never raised. An internal error
    is also handed to `sys.excepthook` (the #10 incident capture)."""
    try:
        step()
    except LIFECYCLE_FAILURES as exc:
        internal = isinstance(exc, INTERNAL_ERRORS)
        message = f"{type(exc).__name__}: {exc}" if internal else describe_failure(exc)
        failure = ConnectionFailure(device, phase, type(exc).__name__, message, internal)
        _log.log(
            logging.ERROR if internal else logging.WARNING,
            "%s: %s failed (%s): %s",
            failure.device,
            failure.phase.value,
            failure.error_type,
            failure.message,
            exc_info=exc,
        )
        if internal:
            _report_internal_error(exc)
        return failure
    return None


def _report_internal_error(exc: Exception) -> None:
    """Hand a contained programming error to the app's incident capture (the apps' #10 hook
    wraps `sys.excepthook`) as if it had escaped -- without letting the hook itself break the
    cleanup that is still running."""
    try:
        sys.excepthook(type(exc), exc, exc.__traceback__)
    except Exception:  # noqa: BLE001 -- a failing hook must not undo the containment
        _log.exception("device connection: the incident hook failed")


def open_device(
    device_name: str,
    device: ConnectableDevice,
    *,
    start: Callable[[], object] | None = None,
    wait: Callable[[], object] | None = None,
) -> ConnectionFailure | None:
    """CONNECT, then START, then WAIT -- all or nothing. On the first failing phase the device
    is released again (`disconnect()`, itself guarded) before the failure is returned, so no
    half-open handle/client/socket survives and the next attempt starts clean. Every port's
    `disconnect()` is safe after a failed or partial `connect()` (port contracts)."""
    for phase, step in (
        (ConnectionPhase.CONNECT, device.connect),
        (ConnectionPhase.START, start),
        (ConnectionPhase.WAIT, wait),
    ):
        if step is None:
            continue
        failure = run_phase(device_name, phase, step)
        if failure is not None:
            run_phase(device_name, ConnectionPhase.DISCONNECT, device.disconnect)
            return failure
    return None


def close_device(
    device_name: str,
    device: ConnectableDevice,
    *,
    cancel: Callable[[], object] | None = None,
) -> list[ConnectionFailure]:
    """CANCEL (when given), then DISCONNECT -- a failing cancel never skips the release (a
    mount whose stop is not confirmed, OnStepAdapter#17, must still give the shared OnStep
    connection back). Never raises; returns what failed."""
    failures: list[ConnectionFailure] = []
    for phase, step in (
        (ConnectionPhase.CANCEL, cancel),
        (ConnectionPhase.DISCONNECT, device.disconnect),
    ):
        if step is None:
            continue
        failure = run_phase(device_name, phase, step)
        if failure is not None:
            failures.append(failure)
    return failures


def stop_each(stops: Iterable[tuple[str, Callable[[], object]]]) -> list[ConnectionFailure]:
    """Run every component's own stop at app exit, each isolated: one raising stop is logged and
    the rest still run (K03: `closeEvent` stopped panels in sequence without isolation)."""
    failures: list[ConnectionFailure] = []
    for name, stop in stops:
        failure = run_phase(name, ConnectionPhase.SHUTDOWN, stop)
        if failure is not None:
            failures.append(failure)
    return failures


@dataclass(frozen=True)
class SwitchResult[DeviceT: ConnectableDevice]:
    """`device`: the newly opened device, None when the switch failed. `failure`: why opening
    the new device failed. `release_failure`: releasing the previous device failed (the
    switch still proceeds). `restore_failure`: re-opening the previous device after a failed
    switch failed too -- the caller falls back to its own default."""

    device: DeviceT | None
    failure: ConnectionFailure | None = None
    release_failure: ConnectionFailure | None = None
    restore_failure: ConnectionFailure | None = None


def switch_device[DeviceT: ConnectableDevice](
    device_name: str,
    previous: DeviceT,
    make_candidate: Callable[[], DeviceT],
    *,
    verify: Callable[[DeviceT], object] | None = None,
    restore_previous: bool = True,
    verify_previous: Callable[[DeviceT], object] | None = None,
) -> SwitchResult[DeviceT]:
    """Issue #45 order: release the PREVIOUS device first (the SDK allows one open handle per
    device -- re-selecting the same physical camera otherwise fails ERROR_BUSY), then build and
    open the candidate (`verify` -- e.g. an identity check -- is its WAIT phase); on any failure
    the candidate is released and, when `restore_previous`, the previous device re-opened
    through the same all-or-nothing `open_device` (`verify_previous`, the previous device's own
    identity check, is that open's WAIT phase; a partial restore is released again)."""
    release_failure = run_phase(device_name, ConnectionPhase.DISCONNECT, previous.disconnect)
    made: list[DeviceT] = []
    failure = run_phase(device_name, ConnectionPhase.CONNECT, lambda: made.append(make_candidate()))
    if failure is None:
        candidate = made[0]
        check = (lambda: verify(candidate)) if verify is not None else None
        failure = open_device(device_name, candidate, wait=check)
        if failure is None:
            return SwitchResult(candidate, release_failure=release_failure)
    restore_failure = None
    if restore_previous:
        check_previous = (
            (lambda: verify_previous(previous)) if verify_previous is not None else None
        )
        restore_failure = open_device(device_name, previous, wait=check_previous)
    return SwitchResult(
        None, failure=failure, release_failure=release_failure, restore_failure=restore_failure
    )


class DeviceConnectionService:
    """One device's connection state for a panel: `connect()` / `disconnect()` never raise;
    `connected`, `last_connect_error` and `last_failure` are what the panel renders and puts in
    its diagnostics. `start` / `wait` / `cancel` are the optional phase steps (see the module
    docstring). A failure in any phase leaves the service disconnected and retryable."""

    def __init__(
        self,
        device_name: str,
        device: ConnectableDevice,
        *,
        start: Callable[[], object] | None = None,
        wait: Callable[[], object] | None = None,
        cancel: Callable[[], object] | None = None,
    ) -> None:
        self._name = device_name
        self._device = device
        self._start = start
        self._wait = wait
        self._cancel = cancel
        self._connected = False
        self._last_connect_error: str | None = None
        self._last_failure: ConnectionFailure | None = None

    @property
    def device_name(self) -> str:
        return self._name

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_connect_error(self) -> str | None:
        """The reason the most recent connect attempt failed; None after a successful one."""
        return self._last_connect_error

    @property
    def last_failure(self) -> ConnectionFailure | None:
        """The most recent failure in any phase (connect, cancel, disconnect, ...)."""
        return self._last_failure

    def connect(self) -> ConnectionFailure | None:
        if self._connected:
            return None
        failure = open_device(self._name, self._device, start=self._start, wait=self._wait)
        if failure is not None:
            self._last_failure = failure
            self._last_connect_error = failure.message
            return failure
        self._connected = True
        self._last_connect_error = None
        return None

    def disconnect(self, *, cancel_motion: bool = False) -> list[ConnectionFailure]:
        """Release the device (after stopping motion first when `cancel_motion`). No-op when not
        connected. The state is disconnected afterwards even when a step failed -- the device
        handle cannot be trusted any more, and a retry must start from a fresh connect."""
        if not self._connected:
            return []
        self._connected = False
        failures = close_device(
            self._name, self._device, cancel=self._cancel if cancel_motion else None
        )
        if failures:
            self._last_failure = failures[-1]
        return failures

    def diagnostic_context(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "last_connect_error": self._last_connect_error,
            "last_connection_failure": (
                self._last_failure.as_dict() if self._last_failure is not None else None
            ),
        }
