"""OnStepMountPulseAdapter — `MountPort` over OnStepAdapter's INDI-backed
finite axis motion (>= this dev build of 0.4.0).

Raw protocol sequencing (GOTO issuance, RA/Dec feedback verification,
size-scaled arrival tolerances) stays inside OnStepAdapter
(`IndiMount.move_ra_axis_deg`/`move_dec_axis_deg`); this shim only maps
axis/direction to a signed degree offset and reports the adapter's verdict.

One real capability gap versus 0.3.5, intentional -- not something this
shim works around locally (AGENTS.md: extend/fix OnStepAdapter, don't build
a parallel implementation here): `pulse_axis` (a timed move at an installed
rate) has **no** INDI equivalent -- the new primitive is a verified
degree-target GOTO, not a duration-at-a-rate pulse.
`capabilities().supports_pulse_guiding` is `False` and `pulse_axis` always
refuses.

`move_angular` works for offsets from 30" to 36000" (10 degrees) --
OnStepAdapter's own bound (raised from an original 720" floor after
OnStepAdapter#14: every seed in AGENTS.md's Mount Align policy now clears
it). Requests outside that range are refused with an explicit message,
never silently clamped.

`install_rate`/`installed_rate` are accepted-but-unused no-ops: the new
primitive takes a target offset directly and needs no arcsec/s rate to
convert a timed pulse into a distance, unlike 0.3.5's `move_ra`/`move_dec`.

Stop during a GOTO (S6.0c). OnStepAdapter 0.4.1's `move_*_axis_deg` blocks for
the whole GOTO (indi_axis_motion.py:164-208, default 30 s timeout) and has no
cancel hook; `move_angular` holds `operation_lock` for all of it (9cea2e9), so
no other compound operation interleaves with it. Stop is the one deliberate
exception: `abort()` does NOT take that lock -- serialized behind the GOTO it
is meant to stop, it could only run once the GOTO had ended. It latches a
cancel flag and hands OnStep's emergency stop (ABORT + TRACK_OFF, then a
confirmation wait of up to 5 s, indi_stop.py:22-88) to a short-lived worker,
so the GUI thread that pressed Stop returns at once. OnStepAdapter itself
calls `emergency_stop` concurrently with a running move (its meridian
supervisor thread, indi_meridian.py:113-115; the mover's own failure path,
indi_axis_motion.py:246-248), takes none of the mover's locks for it, and its
transport is thread-safe (indi_transport.py:35-36, 283-289). The interrupted
call still runs on to OnStepAdapter's own deadline and returns a refusal that
names the stop; the lock is released then (bounded), never earlier.
`status()` never queues behind a running operation: it serves its last
reading (with `slewing=True` while this adapter's GOTO is in flight) instead
of reading the controller mid-operation; with nothing read yet it claims
nothing (`connected=False`). That is for display only, and marked so
(`MountStatus.fresh=False`, S6.5): no decision may use it.

Angular bounds (S6.5, #55 C02): `capabilities()` reports `supports_angular_moves` and
OnStepAdapter's own `IndiAxisMover` bound as `min_angular_arcsec`/`max_angular_arcsec`
(single source: `onstep_adapter.indi_axis_motion`); `move_angular` refuses outside exactly
that range. The `min_angular_arcsec` property is a deprecated alias for duck-typed callers.

Guide pulses while tracking (S6.0d, #39). OnStepAdapter 0.5.0 (unpublished at the time of
writing; main stays pinned to 0.4.1) adds `IndiMount.guide_pulse(direction, duration_ms)`
(indi_mount.py:32-38): standard INDI timed-guide properties, tracking left ON, 20..5000 ms
(indi_guiding.py:17-18, ValueError outside, :149-152) in self-terminating 500 ms chunks (:19,
:156-164) with a full safety preflight before every chunk and after the last (:178-212, refusing
at PARK/HOME/slew/fault/limit/not tracking, meridian hard stop or firmware limit, and -- under
the default "strict" `tracking_authority_policy` -- without time/site/HOME authority), one pulse
at a time (a second concurrent call answers "another guide pulse is active", :166-170), and an
`emergency_stop()` -- ABORT + TRACK_OFF -- whenever a failure follows an issued chunk (:213-226).
Capability: detected once per connection on `connect()` from the installed OnStepAdapter -- its
mount facade has `guide_pulse` AND its package exports the bounds (0.5.0 `__init__.py`:
MIN_GUIDE_PULSE_MS/MAX_GUIDE_PULSE_MS); reported as the separate
`supports_guide_pulses_while_tracking`, never as `supports_pulse_guiding` (Mount Align's
timed/angular choice is unaffected). `guide_pulse` runs under `operation_lock` like a GOTO, is
refused unsent while a Stop is latched, and is covered by Stop exactly like a GOTO (the stop
worker's emergency stop turns tracking OFF; 0.5.0's next chunk preflight then refuses). After a
pulse that was sent and then failed it reads the mount status once more, inside the lock, so a
caller learns whether tracking is now OFF (`GuidePulseResult.tracking_off`) -- 0.5.0's own
`tracking_preserved` comes from the snapshot taken BEFORE its emergency stop (:220-222).
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Protocol

import onstep_adapter
from onstep_adapter import IndiMount
from onstep_adapter.indi_axis_motion import MAX_AXIS_MOVE_DEG, MIN_AXIS_MOVE_ARCSEC

from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    GuidePulseResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.onstep.connection import OnStepConnection

#: OnStepAdapter's own `IndiAxisMover` bound (30"..10 degrees), taken from OnStepAdapter
#: itself (single source). Reported to callers as `MountCapabilities.min_angular_arcsec` /
#: `max_angular_arcsec` (S6.5); `MIN_AXIS_ARCSEC` stays re-exported for this app.
MIN_AXIS_ARCSEC = float(MIN_AXIS_MOVE_ARCSEC)
_MAX_AXIS_ARCSEC = float(MAX_AXIS_MOVE_DEG) * 3600.0

_log = logging.getLogger(__name__)

#: Every refusal caused by Stop starts with this (the runner and panel report it as-is).
_STOPPED = "stopped by the user"

#: S6.0d: what a guide pulse needs from the installed OnStepAdapter (>= 0.5.0).
NO_GUIDE_PULSE_API = (
    "the installed OnStepAdapter has no guide pulse while tracking (needs >= 0.5.0)"
)

#: S6.0d: axis/direction -> 0.5.0 guide direction (same sign convention as `move_angular`:
#: AXIS1 POSITIVE increases hour angle = west, AXIS2 POSITIVE = north).
_GUIDE_DIRECTION = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): "west",
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): "east",
    (MountAxis.AXIS2, AxisDirection.POSITIVE): "north",
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): "south",
}


class _GuidePulseOutcome(Protocol):
    """The fields of 0.5.0's `IndiGuidePulseResult` this shim reads (indi_guiding.py:53-65)."""

    chunks_requested: int
    chunks_completed: int
    command_accepted: bool
    pulse_completed: bool
    tracking_preserved: bool
    warnings: tuple[str, ...]
    error: str | None


#: Why tracking is OFF after a guide pulse that did not complete (review fix 2).
_USER_STOP_CAUSE = "the user's Stop sends OnStep's emergency stop: ABORT + TRACK_OFF"
_FAILED_PULSE_CAUSE = "OnStepAdapter stops the mount when a sent guide pulse fails"
#: 0.5.0 appends this when its own emergency stop raised (indi_guiding.py:217-219).
_STOP_FAILED_MARK = "emergency stop failed"
#: Bound on waiting for a latched Stop's worker before reading tracking: OnStepAdapter's own
#: stop confirmation wait is <= 5 s (indi_stop.py:22-28) plus margin.
_STOP_WORKER_WAIT_S = 6.0


def _installed_guide_pulse_range_ms() -> tuple[int, int] | None:
    """The guide-pulse bounds the installed OnStepAdapter exports (0.5.0), or None (0.4.1)."""
    low = getattr(onstep_adapter, "MIN_GUIDE_PULSE_MS", None)
    high = getattr(onstep_adapter, "MAX_GUIDE_PULSE_MS", None)
    if isinstance(low, int) and isinstance(high, int) and 0 < low <= high:
        return (low, high)
    return None


_NO_PULSE_PRIMITIVE = (
    "OnStepAdapter has no timed pulse primitive over INDI yet "
    "(tracked as an OnStepAdapter enhancement request)"
)


class OnStepMountPulseAdapter:
    def __init__(self, connection: OnStepConnection) -> None:
        self._connection = connection
        self._held = False
        #: Latched by `abort()`, cleared only by `clear_abort()` (the next operator command):
        #: a Stop also refuses the remaining steps of the sequence it interrupted.
        self._cancel = threading.Event()
        self._last_status: MountStatus | None = None
        #: Guards the stop bookkeeping below; never held while talking to the mount.
        self._state = threading.Lock()
        #: Generation of the last GOTO issued (incremented atomically with the cancel check).
        self._move_generation = 0
        #: Generation of the GOTO call in flight right now, or None.
        self._in_flight_generation: int | None = None
        #: A Stop not yet served by the stop worker: the newest move generation it covers.
        self._pending_stop: int | None = None
        self._stop_worker: threading.Thread | None = None
        #: S6.0d: the operation in flight is a guide pulse, not a GOTO (status() must not say
        #: "slewing" for it).
        self._in_flight_is_guide = False
        #: S6.0d: guide-pulse bounds of the connected OnStepAdapter; None = no capability.
        self._guide_range_ms: tuple[int, int] | None = None
        #: Accepted but never consulted -- see module docstring.
        self._rates: dict[tuple[MountAxis, AxisDirection], float] = {}

    def connect(self) -> None:
        if not self._held:
            client = self._connection.acquire()
            self._held = True
            # S6.0d: detected once per connection from the installed OnStepAdapter.
            has_api = callable(getattr(client.mount, "guide_pulse", None))
            self._guide_range_ms = _installed_guide_pulse_range_ms() if has_api else None

    def disconnect(self) -> None:
        if self._held:
            self._held = False
            self._guide_range_ms = None
            self._last_status = None  # S6.0c: no stale carry-over into the next session
            self._connection.release()

    @property
    def status_pending(self) -> bool:
        """S6.0c: connected, but no reading yet because another operation holds the connection
        (`status()` then says `connected=False`) -- lets a UI say "busy", not "disconnected"."""
        return self._held and self._last_status is None and self._mount() is not None

    @property
    def _goto_in_flight(self) -> bool:
        return self._in_flight_generation is not None and not self._in_flight_is_guide

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            supports_pulse_guiding=False,
            min_pulse_ms=0,
            max_pulse_ms=0,
            supports_guide_pulses_while_tracking=self.guide_pulse_range_ms is not None,
            supports_angular_moves=True,
            min_angular_arcsec=MIN_AXIS_ARCSEC,
            max_angular_arcsec=_MAX_AXIS_ARCSEC,
        )

    @property
    def guide_pulse_range_ms(self) -> tuple[int, int] | None:
        """S6.0d: (min, max) ms of one guide pulse, from the connected OnStepAdapter."""
        return self._guide_range_ms if self._held else None

    def _mount(self) -> IndiMount | None:
        client = self._connection.client
        return client.mount if self._held and client is not None else None

    def status(self) -> MountStatus:
        # Real field report (diagnostic 7b21bdf1): see OnStepConnection's
        # own module docstring -- move_angular() below runs on a worker
        # thread (calibration/nudge) while this status() is polled from
        # the GUI thread; serialize both against every other adapter
        # sharing this connection so they can never interleave mid-move.
        # S6.0c: ...and never queue behind one either (it is polled on the GUI thread).
        with self._connection.try_operation() as entered:
            mount = self._mount()
            if mount is None:
                return MountStatus(connected=False, tracking=False, slewing=False)
            if entered:
                snapshot = mount.get_status()
                self._last_status = MountStatus(
                    connected=True, tracking=snapshot.tracking, slewing=snapshot.slewing
                )
                return self._last_status
            last = self._last_status
            if last is None:  # busy and nothing read yet: claim nothing (S6.0c)
                return MountStatus(
                    connected=False, tracking=False, slewing=self._goto_in_flight, fresh=False
                )
            return MountStatus(
                connected=True,
                tracking=last.tracking,
                slewing=last.slewing or self._goto_in_flight,
                fresh=False,  # held over (S6.0c): display only
            )

    def abort(self) -> None:
        """Stop: returns at once on any thread (see the module docstring, S6.0c).

        Latches the cancel flag (a move still queued for the connection, or the next step of
        the interrupted sequence, is refused unsent) and has the stop worker send OnStep's
        emergency stop WITHOUT `operation_lock` -- the deliberate exception to 9cea2e9's
        serialization, because the GOTO it must stop holds that lock until it ends. Every
        call is served: a Stop while the worker is busy queues one more pass."""
        mount = self._mount()
        with self._state:
            self._cancel.set()
            if mount is None:
                return
            # Every GOTO up to this generation may still be (or start) moving; none after it
            # can start until `clear_abort()` (the cancel check is atomic with the increment).
            self._pending_stop = self._move_generation
            worker = self._stop_worker
            if worker is not None and worker.is_alive():
                return  # the running worker picks this request up after its current pass
            worker = threading.Thread(
                target=self._stop_worker_loop, args=(mount,), daemon=True, name="onstep-mount-stop"
            )
            self._stop_worker = worker
            # Started inside the critical section (re-review P-e): a concurrent abort() sees
            # this worker alive and only queues a pass, never starts a second worker.
            worker.start()

    def _stop_worker_loop(self, mount: IndiMount) -> None:
        while True:
            with self._state:
                covered = self._pending_stop
                self._pending_stop = None
                if covered is None:
                    self._stop_worker = None
                    return
            self._stop_pass(mount, covered)

    def _stop_pass(self, mount: IndiMount, covered: int) -> None:
        """One Stop: an emergency stop now, and -- only if a GOTO this Stop covers is STILL in
        flight once that stop is confirmed (it passed the cancel check just before Stop and
        may have issued its GOTO after the first ABORT) -- one more. A GOTO started after a
        later `clear_abort()` (the operator's next command) is never touched."""
        self._send_one_stop(mount)
        with self._state:
            in_flight = self._in_flight_generation
        if in_flight is not None and in_flight <= covered:
            self._send_one_stop(mount)

    @staticmethod
    def _send_one_stop(mount: IndiMount) -> None:
        try:
            result = mount.stop()
        except Exception:  # noqa: BLE001 -- the worker must never die silently; flag stays set
            _log.exception("Stop: OnStep emergency stop failed")
            return
        if not result.stopped_confirmed:
            _log.warning("Stop: OnStep did not confirm the stop: %s", result.errors)

    def clear_abort(self) -> None:
        """Re-arm after a Stop: called when the operator starts the next command."""
        with self._state:
            self._cancel.clear()

    # ---- AngularMotionPort ------------------------------------------------
    @property
    def min_angular_arcsec(self) -> float:
        """Deprecated alias (S6.5) of `capabilities().min_angular_arcsec`: the smallest
        `move_angular` size OnStepAdapter accepts. Kept for the duck-typed callers
        (`getattr(mount, "min_angular_arcsec", 0.0)`) until they read the capability."""
        return self.capabilities().min_angular_arcsec

    def installed_rate(self, axis: MountAxis, direction: AxisDirection) -> float | None:
        return self._rates.get((axis, direction))

    def install_rate(self, axis: MountAxis, direction: AxisDirection, arcsec_per_s: float) -> None:
        """Recorded for API compatibility only -- OnStepAdapter's degree-target
        move needs no rate. Still validates input, matching 0.3.5's contract."""
        if not (0.0 < arcsec_per_s < float("inf")):
            raise ValueError(f"invalid centering rate {arcsec_per_s!r}")
        self._rates[(axis, direction)] = float(arcsec_per_s)

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        mount = self._mount()
        if mount is None:
            return CommandResult(accepted=False, message="not connected")
        if not (arcsec > 0.0) or not math.isfinite(arcsec):
            return CommandResult(accepted=False, message=f"invalid angular size {arcsec!r}")
        if arcsec < MIN_AXIS_ARCSEC or arcsec > _MAX_AXIS_ARCSEC:
            return CommandResult(
                accepted=False,
                message=(
                    f"{arcsec:.1f}\" is outside OnStepAdapter's supported axis-move range "
                    f"({MIN_AXIS_ARCSEC:.0f}\"-{_MAX_AXIS_ARCSEC:.0f}\")"
                ),
            )
        positive = direction is AxisDirection.POSITIVE
        offset_deg = (arcsec if positive else -arcsec) / 3600.0
        with self._connection.operation_lock:
            # S6.0c: checked once the connection is ours -- a Stop that arrived while this
            # move was queued (or between two steps of a sequence) means: send nothing.
            with self._state:  # atomic with abort(): see there
                if self._cancel.is_set():
                    return CommandResult(accepted=False, message=_STOPPED + " -- move not sent")
                self._move_generation += 1
                self._in_flight_generation = self._move_generation
            try:
                if axis == MountAxis.AXIS1:
                    mount.move_ra_axis_deg(offset_deg)
                else:
                    mount.move_dec_axis_deg(offset_deg)
            except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
                if self._cancel.is_set():
                    return CommandResult(accepted=False, message=f"{_STOPPED} ({exc})")
                return CommandResult(accepted=False, message=str(exc))
            finally:
                with self._state:
                    self._in_flight_generation = None
        return CommandResult(accepted=True)

    # ---- GuidePulsePort (S6.0d) ------------------------------------------
    def guide_pulse(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int
    ) -> GuidePulseResult:
        """One bounded guide pulse with tracking left ON (see the module docstring)."""
        mount = self._mount()
        if mount is None:
            return GuidePulseResult(accepted=False, message="not connected")
        bounds = self._guide_range_ms
        pulse = getattr(mount, "guide_pulse", None)
        if bounds is None or not callable(pulse):
            return GuidePulseResult(accepted=False, message=NO_GUIDE_PULSE_API)
        low, high = bounds
        if (
            isinstance(duration_ms, bool)
            or not isinstance(duration_ms, int)
            or not low <= duration_ms <= high
        ):
            return GuidePulseResult(
                accepted=False,
                message=(
                    f"guide pulse {duration_ms!r} ms is outside OnStepAdapter's supported "
                    f"range ({low}-{high} ms) -- nothing was sent"
                ),
            )
        with self._connection.operation_lock:
            with self._state:  # atomic with abort(), as for a GOTO
                stopped = self._cancel.is_set()
                if not stopped:
                    self._move_generation += 1
                    self._in_flight_generation = self._move_generation
                    self._in_flight_is_guide = True
                worker = self._stop_worker
            if stopped:
                return self._latched_stop_result(mount, worker)
            error = ""
            raw: _GuidePulseOutcome | None = None
            try:
                raw = pulse(_GUIDE_DIRECTION[(axis, direction)], duration_ms)
            except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
                error = str(exc)
            finally:
                with self._state:
                    self._in_flight_generation = None
                    self._in_flight_is_guide = False
            if raw is None:
                if self._cancel.is_set():
                    error = f"{_STOPPED} ({error})"
                return GuidePulseResult(accepted=False, message=error)
            return self._guide_result(mount, raw)

    def _guide_result(self, mount: IndiMount, raw: _GuidePulseOutcome) -> GuidePulseResult:
        """Normalize 0.5.0's `IndiGuidePulseResult` (indi_guiding.py:53-65). The caller holds
        `operation_lock`, so the follow-up status read is a fresh one."""
        sent = bool(raw.command_accepted)
        warnings = tuple(raw.warnings)
        requested, done = int(raw.chunks_requested), int(raw.chunks_completed)
        if raw.pulse_completed:
            return GuidePulseResult(
                accepted=True,
                sent=True,
                tracking_preserved=bool(raw.tracking_preserved),
                warnings=warnings,
                chunks_requested=requested,
                chunks_completed=done,
            )
        message = str(raw.error or "guide pulse not completed")
        stopped = self._cancel.is_set()
        if stopped:
            message = f"{_STOPPED} ({message})"
        tracking_off = False
        if sent:
            tracking_off, note = self._tracking_note(
                mount,
                cause=_USER_STOP_CAUSE if stopped else _FAILED_PULSE_CAUSE,
                chunks=(done, requested),
                stop_unconfirmed=_STOP_FAILED_MARK in message,
            )
            message += note
        return GuidePulseResult(
            accepted=False,
            message=message,
            sent=sent,
            tracking_preserved=bool(raw.tracking_preserved),
            tracking_off=tracking_off,
            warnings=warnings,
            chunks_requested=requested,
            chunks_completed=done,
        )

    def _latched_stop_result(
        self, mount: IndiMount, worker: threading.Thread | None
    ) -> GuidePulseResult:
        """Review fix 1: the latched Stop sent OnStep's emergency stop (ABORT + TRACK_OFF) --
        nothing is sent, and the result says whether the mount still tracks. The stop worker
        gets a bounded moment to finish first (its confirmation wait, indi_stop.py:52-80); still
        running then means "unknown", i.e. possibly stopped."""
        if worker is not None and worker.is_alive():
            worker.join(_STOP_WORKER_WAIT_S)
        if worker is not None and worker.is_alive():
            tracking_off, note = True, (
                " -- tracking may be OFF, check the mount (the Stop is still in progress)"
            )
        else:
            tracking_off, note = self._tracking_note(mount, cause=_USER_STOP_CAUSE)
        return GuidePulseResult(
            accepted=False,
            message=_STOPPED + " -- guide pulse not sent" + note,
            tracking_off=tracking_off,
        )

    @staticmethod
    def _tracking_note(
        mount: IndiMount,
        *,
        cause: str,
        chunks: tuple[int, int] | None = None,
        stop_unconfirmed: bool = False,
    ) -> tuple[bool, str]:
        """(tracking_off, text to append) after a pulse that a stop may have ended. Unknown
        counts as possibly stopped (review fix 3): an unreadable status, or 0.5.0 reporting
        that its own emergency stop failed, says "tracking may be OFF -- check the mount"."""
        try:
            tracking = bool(mount.get_status().tracking)
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            return True, f" -- tracking may be OFF, check the mount (status unreadable: {exc})"
        if stop_unconfirmed:
            return True, (
                " -- tracking may be OFF, check the mount (OnStepAdapter's emergency stop was "
                "not confirmed)"
            )
        if tracking:
            return False, ""
        where = f"after {chunks[0]} of {chunks[1]} chunk(s) " if chunks else ""
        return True, f" -- {where}the mount is NOT tracking any more ({cause})"

    # ---- MountPort ----------------------------------------------------
    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        return CommandResult(accepted=False, message=_NO_PULSE_PRIMITIVE)
