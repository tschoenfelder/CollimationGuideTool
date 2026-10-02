"""OnStepFocuserAdapter — `FocuserPort` over OnStepAdapter's `IndiFocuser`
(>= 0.4.0, INDI-backed).

S6.0c: the reads FocuserPanel polls on the GUI thread (`status()`, and
`is_available`/`is_moving()` derived from it) never queue behind another
thread's operation on the shared connection (e.g. a mount axis GOTO holding
`operation_lock` for up to 30 s): when the lock is busy they serve the last
reading; before any reading exists, "unavailable". `FocuserStatus` cannot
say a reading is held over (S6.5 capability item). Moves, stop and the
`blockers()` diagnostic still block on the lock.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import replace

from onstep_adapter import IndiFocuser

from astrotool_core.focus.port import FocuserMoveResult, FocuserPort, FocuserStatus
from astrotool_core.onstep.connection import OnStepConnection

_log = logging.getLogger(__name__)

_UNAVAILABLE = FocuserStatus(available=False, position=0, max_position=0, moving=False)


class OnStepFocuserAdapter(FocuserPort):
    def __init__(self, connection: OnStepConnection) -> None:
        self._connection = connection
        self._held = False
        #: S6.0c: the last reading, served while another operation holds the connection.
        self._last_status: FocuserStatus | None = None
        #: True while this adapter's own move holds the connection: a served reading says moving.
        self._moving = False

    def connect(self) -> None:
        if not self._held:
            self._connection.acquire()
            self._held = True

    def disconnect(self) -> None:
        if self._held:
            self._held = False
            self._last_status = None  # S6.0c: no stale carry-over into the next session
            self._connection.release()

    def _focuser(self) -> IndiFocuser | None:
        """The live focuser, or None when not connected."""
        client = self._connection.client
        if not self._held or client is None or client.focuser is None:
            return None
        return client.focuser

    @property
    def is_available(self) -> bool:
        return self.status().available

    def status(self) -> FocuserStatus:
        # Real field report (diagnostic 7b21bdf1): see OnStepConnection's
        # own module docstring -- every operation here is serialized
        # against every other adapter sharing this connection so a
        # GUI-thread poll can never interleave mid-sequence with a
        # worker-thread move_absolute(). S6.0c: ...without ever queueing behind one.
        with self._connection.try_operation() as entered:
            focuser = self._focuser()
            if focuser is None:
                return _UNAVAILABLE
            if not entered:
                last = self._last_status
                if last is None:
                    return _UNAVAILABLE
                return replace(last, moving=last.moving or self._moving)
            s = focuser.get_status()
            available = "indi_device_disconnected" not in s.blockers and s.position is not None
            maximum = s.driver_maximum or s.configured_maximum or 0
            self._last_status = FocuserStatus(available, s.position or 0, maximum, bool(s.moving))
            return self._last_status

    def blockers(self) -> tuple[str, ...]:
        """The live `IndiFocuserSnapshot.blockers` this instant -- e.g.
        `focuser_position_stale`, `focuser_configured_maximum_missing` --
        never collapsed to a single bool the way `status().available` is.
        `FocuserStatus`/`FocuserPort` stay hardware-neutral (same
        reasoning as `FocuserMoveResult`'s own docstring); this is an
        adapter-only diagnostic extra, duck-typed by
        `FocuserPanel.diagnostic_context()` so a rejected move's real
        precondition is visible in the next diagnostic bundle even when
        `move_ready` was already False *before* the move was attempted --
        not just the `IndiFocuserMoveResult.error` a rejected move itself
        produces. Empty when not connected."""
        with self._connection.operation_lock:
            focuser = self._focuser()
            if focuser is None:
                return ()
            return tuple(focuser.get_status().blockers)

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        with self._connection.operation_lock:
            focuser = self._focuser()
            if focuser is None:
                return FocuserMoveResult(accepted=False, target_position=steps, start_position=0)
            start = focuser.get_status().position or 0
            self._moving = True
            try:
                r = focuser.move_absolute(steps)
            except ValueError as exc:
                # `FocuserMoveResult` is deliberately hardware-neutral (see
                # its own docstring) and carries no reason field -- log it
                # here instead, so a rejected move is at least diagnosable
                # from application.log (and hence a diagnostic bundle) even
                # though the caller only ever sees `accepted=False`.
                _log.warning("OnStepFocuserAdapter: move to %s rejected: %s", steps, exc)
                return FocuserMoveResult(
                    accepted=False, target_position=steps, start_position=start
                )
            finally:
                self._moving = False
            if not r.reached and r.error:
                _log.warning("OnStepFocuserAdapter: move to %s rejected: %s", steps, r.error)
            return FocuserMoveResult(r.reached, r.target, start)

    def move(self, steps: int) -> None:
        """Relative move — 0.4.0's `IndiFocuser` only exposes an absolute
        target, so this synthesizes one from the last known position."""
        with self._connection.operation_lock:
            focuser = self._focuser()
            if focuser is None:
                return
            current = focuser.get_status().position or 0
            maximum = (
                focuser.get_status().driver_maximum or focuser.get_status().configured_maximum
            )
            target = current + steps
            if maximum is not None:
                target = max(0, min(maximum, target))
            self._moving = True
            try:
                with contextlib.suppress(ValueError):
                    focuser.move_absolute(target)
            finally:
                self._moving = False

    def get_position(self) -> int:
        with self._connection.operation_lock:
            focuser = self._focuser()
            return 0 if focuser is None else int(focuser.get_status().position or 0)

    def get_max_position(self) -> int:
        with self._connection.operation_lock:
            focuser = self._focuser()
            if focuser is None:
                return 0
            s = focuser.get_status()
            return int(s.driver_maximum or s.configured_maximum or 0)

    def is_moving(self) -> bool:
        return self.status().moving  # S6.0c: polled on the GUI thread -- never queues

    def stop(self) -> None:
        with self._connection.operation_lock:
            focuser = self._focuser()
            if focuser is not None:
                focuser.stop()
