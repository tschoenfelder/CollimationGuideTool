"""OnStepFocuserAdapter — `FocuserPort` over OnStepAdapter's `IndiFocuser`
(>= 0.4.0, INDI-backed).

S6.0c: the reads FocuserPanel polls on the GUI thread (`status()`, and
`is_available`/`is_moving()` derived from it) never queue behind another
thread's operation on the shared connection (e.g. a mount axis GOTO holding
`operation_lock` for up to 30 s): when the lock is busy they serve the last
reading; before any reading exists, "unavailable". `FocuserStatus` cannot
say a reading is held over (S6.5 capability item). Moves still serialize on the lock.

Stop during a move (S6.8c, R7 -- the S6.0c pattern for the focuser). OnStepAdapter 0.5.0's
`IndiFocuser.move_absolute` blocks until the target is confirmed (indi_focuser.py:78-135,
default 30 s), and the moves here hold `operation_lock` for all of it. `IndiFocuser.stop()` is
built to be called while that move runs: it sets `_cancel_requested` without taking the
focuser's `_motion_lock` (:137-139), the move's poll loop checks the flag on every pass of at
most 0.5 s (:107-114) and then stops and returns a refusal (:130-135). So `stop()` here is the
deliberate exception to 9cea2e9's serialization: it takes no `operation_lock` (behind the move
it should stop it could only run once that move had ended). It refuses every move requested
up to now that has not been sent yet (one queued behind a mount GOTO, say), and hands
OnStepAdapter's stop -- ABORT plus a confirmation wait of up to 5 s (:141-166) -- to a
short-lived worker, so the GUI thread that pressed Stop returns at once. A move re-checks,
just before it is sent, whether a Stop now covers it (refused unsent). Past that check a Stop
can still land before OnStepAdapter clears the cancel flag on entry (:84) and be lost, so while
a move this Stop covers is still in flight after a stop pass, the worker stops again -- at most
`_MAX_STOP_PASSES` passes (each waits up to OnStepAdapter's 5 s confirmation, which gives the
move time to get past :84). A move requested after the Stop runs normally. `disconnect()`
waits (bounded) for a stop still being delivered, so a quit never closes the client under it.
`blockers()` (a GUI-thread diagnostic) never queues behind a move either: while the
connection is busy it says it could not read.

S6.3 (#52 K02): every device-side failure OnStepAdapter can raise
(`ONSTEP_OPERATION_ERRORS`, one owner) becomes a result here -- a rejected move, a
logged no-op relative move/stop, an "unavailable" status -- instead of escaping into the
autofocus worker or a Qt slot (before, only ValueError was converted; an INDI TimeoutError
from a move left the autofocus runner busy forever, audit L02).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace

from onstep_adapter import IndiFocuser

from astrotool_core.focus.port import FocuserMoveResult, FocuserPort, FocuserStatus
from astrotool_core.onstep.connection import OnStepConnection
from astrotool_core.onstep.errors import ONSTEP_OPERATION_ERRORS

_log = logging.getLogger(__name__)

_UNAVAILABLE = FocuserStatus(available=False, position=0, max_position=0, moving=False)
#: `blockers()` while another operation holds the connection (S6.8c): not read, not "clear".
BLOCKERS_NOT_READ = "not_read_connection_busy"
#: The stop worker's passes for one Stop request, at most (see the module docstring).
_MAX_STOP_PASSES = 3
#: `disconnect()` waits at most this long (real time) for a Stop still being delivered: every
#: pass's OnStepAdapter 5 s stop confirmation (indi_focuser.py:137) plus a margin.
_STOP_DELIVERY_WAIT_S = 5.0 * _MAX_STOP_PASSES + 2.0


class OnStepFocuserAdapter(FocuserPort):
    def __init__(self, connection: OnStepConnection) -> None:
        self._connection = connection
        self._held = False
        #: S6.0c: the last reading, served while another operation holds the connection.
        self._last_status: FocuserStatus | None = None
        #: True while this adapter's own move holds the connection: a served reading says moving.
        self._moving = False
        #: S6.8c Stop bookkeeping, guarded by `_state` (never by `operation_lock`): moves are
        #: numbered when requested; a Stop refuses every move numbered up to then that is not
        #: in flight yet, and its worker re-stops a covered move still in flight.
        self._state = threading.Lock()
        self._move_generation = 0
        self._stopped_through = 0
        self._in_flight: int | None = None
        self._pending_stop: int | None = None
        self._stop_worker: threading.Thread | None = None

    def connect(self) -> None:
        if not self._held:
            self._connection.acquire()
            self._held = True

    def disconnect(self) -> None:
        if self._held:
            self._wait_for_stop_delivery()
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
            try:
                s = focuser.get_status()
            except ONSTEP_OPERATION_ERRORS as exc:
                _log.warning("OnStepFocuserAdapter: status read failed: %s", exc)
                self._last_status = None
                return _UNAVAILABLE
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
        with self._connection.try_operation() as entered:
            focuser = self._focuser()
            if focuser is None:
                return ()
            if not entered:  # S6.8c: never queue behind a move on the GUI thread
                return (BLOCKERS_NOT_READ,)
            return tuple(focuser.get_status().blockers)

    # ---- S6.8c: Stop bookkeeping -----------------------------------------
    def _request_move(self) -> int:
        """Number a move when it is requested (before it may queue for the connection)."""
        with self._state:
            self._move_generation += 1
            return self._move_generation

    def _enter_move(self, generation: int) -> bool:
        """Under `operation_lock`: False when a Stop since the request covers this move (it is
        then refused unsent); else it is in flight -- atomic with `stop()`'s bookkeeping."""
        with self._state:
            if generation <= self._stopped_through:
                return False
            self._in_flight = generation
            return True

    def _covered_by_stop(self, generation: int) -> bool:
        """Re-check just before sending: a Stop since `_enter_move` covers this move."""
        with self._state:
            return generation <= self._stopped_through

    def _leave_move(self) -> None:
        with self._state:
            self._in_flight = None

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        generation = self._request_move()
        with self._connection.operation_lock:
            focuser = self._focuser()
            if focuser is None:
                return FocuserMoveResult(accepted=False, target_position=steps, start_position=0)
            if not self._enter_move(generation):
                _log.info("OnStepFocuserAdapter: move to %s cancelled by Stop, not sent", steps)
                return FocuserMoveResult(accepted=False, target_position=steps, start_position=0)
            try:
                try:
                    start = focuser.get_status().position or 0
                except ONSTEP_OPERATION_ERRORS as exc:
                    _log.warning("OnStepFocuserAdapter: move to %s rejected: %s", steps, exc)
                    return FocuserMoveResult(
                        accepted=False, target_position=steps, start_position=0
                    )
                if self._covered_by_stop(generation):
                    _log.info("OnStepFocuserAdapter: move to %s cancelled by Stop, not sent", steps)
                    return FocuserMoveResult(
                        accepted=False, target_position=steps, start_position=start
                    )
                self._moving = True
                try:
                    r = focuser.move_absolute(steps)
                except ONSTEP_OPERATION_ERRORS as exc:
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
            finally:
                self._leave_move()
            if not r.reached and r.error:
                _log.warning("OnStepFocuserAdapter: move to %s rejected: %s", steps, r.error)
            return FocuserMoveResult(r.reached, r.target, start)

    def move(self, steps: int) -> None:
        """Relative move — 0.4.0's `IndiFocuser` only exposes an absolute
        target, so this synthesizes one from the last known position."""
        generation = self._request_move()
        with self._connection.operation_lock:
            focuser = self._focuser()
            if focuser is None:
                return
            if not self._enter_move(generation):
                _log.info("OnStepFocuserAdapter: relative move by %s cancelled by Stop", steps)
                return
            self._moving = True
            try:
                status = focuser.get_status()
                current = status.position or 0
                maximum = status.driver_maximum or status.configured_maximum
                target = current + steps
                if maximum is not None:
                    target = max(0, min(maximum, target))
                if self._covered_by_stop(generation):
                    _log.info("OnStepFocuserAdapter: relative move by %s cancelled by Stop", steps)
                    return
                focuser.move_absolute(target)
            except ONSTEP_OPERATION_ERRORS as exc:
                # Fire-and-forget (FocuserPanel's jog worker, S6.8c): never raises.
                _log.warning("OnStepFocuserAdapter: relative move by %s failed: %s", steps, exc)
            finally:
                self._moving = False
                self._leave_move()

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
        """Stop: returns at once on any thread, never takes `operation_lock` (S6.8c, see the
        module docstring). Every call is served: a Stop while the worker is busy queues one
        more pass."""
        focuser = self._focuser()
        with self._state:
            # Every move requested up to now is covered: refused if not yet in flight.
            self._stopped_through = self._move_generation
            if focuser is None:
                return
            self._pending_stop = self._move_generation
            if self._stop_worker is not None:
                return  # the running worker picks this request up after its current pass
            worker = threading.Thread(
                target=self._stop_worker_loop,
                args=(focuser,),
                daemon=True,
                name="onstep-focuser-stop",
            )
            self._stop_worker = worker
            # Started inside the critical section: a concurrent stop() sees it and only
            # queues a pass, never starts a second worker.
            worker.start()

    def _stop_worker_loop(self, focuser: IndiFocuser) -> None:
        while True:
            with self._state:
                covered = self._pending_stop
                self._pending_stop = None
                if covered is None:
                    self._stop_worker = None
                    return
            for _ in range(_MAX_STOP_PASSES):
                self._send_one_stop(focuser)
                with self._state:
                    in_flight = self._in_flight
                if in_flight is None or in_flight > covered:
                    break
                # A covered move is still in flight: it may have cleared OnStepAdapter's cancel
                # flag on entry (indi_focuser.py:84) after this pass set it -- stop again. Not
                # guaranteed to be past :84 yet; bounded by _MAX_STOP_PASSES.
            else:
                _log.warning(
                    "OnStepFocuserAdapter: a stopped move was still in flight after %d stops",
                    _MAX_STOP_PASSES,
                )

    @staticmethod
    def _send_one_stop(focuser: IndiFocuser) -> None:
        try:
            confirmed = focuser.stop()
        except ONSTEP_OPERATION_ERRORS as exc:
            _log.warning("OnStepFocuserAdapter: stop failed: %s", exc)
            return
        except Exception:  # noqa: BLE001 -- the worker must never die silently
            _log.exception("OnStepFocuserAdapter: stop failed")
            return
        if confirmed is False:
            _log.warning("OnStepFocuserAdapter: the focuser did not confirm the stop")

    def _wait_for_stop_delivery(self) -> None:
        """Bounded (real time): a quit is stop() then disconnect(), and the release may close
        the client -- the abort must have reached the controller first."""
        with self._state:
            worker = self._stop_worker
        if worker is None or worker is threading.current_thread():
            return
        worker.join(_STOP_DELIVERY_WAIT_S)
        if worker.is_alive():
            _log.warning(
                "OnStepFocuserAdapter: stop still running after %.0f s; disconnecting anyway",
                _STOP_DELIVERY_WAIT_S,
            )
