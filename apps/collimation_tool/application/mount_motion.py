"""The one mount-motion executor (S6.8a, movement-paths R3/R1/R4/R8/F12; #55, #31, #46, #44).

Every RA/Dec motion of the app -- Mount Align's background sequence (calibration, nudge, screen
move, the return after an aborted calibration), guide-assisted reacquisition (angular and guide
pulse) and the Mount panel's park/unpark -- runs under a `MotionClaim` taken from ONE
`MountMotionExecutor` (MainWindow owns it). It owns:

- **One in-flight mount motion app-wide.** `claim()` refuses a second motion with a busy reason
  naming the motion in flight; nothing is queued silently. The caller reports that reason.
- **The Stop latch / re-arm.** An accepted claim is a new operator command: it re-arms the mount
  adapter's latched Stop (`rearm_after_stop`). `abort()` returns at once on any thread: it cancels
  the claim in flight (every wait taking its `cancel` event ends at once -- settles, park polls,
  retries) and hands the mount's own lock-free `abort()` the emergency stop (S6.0c), so a running
  GOTO is stopped instead of waited for. Fine-collimation Cancel (R4), Mount Align Stop and quit
  (R8) all call it.
- **The tracking rule (R1).** An axis GOTO never stops tracking to make itself possible:
  `axis_move_gate` asks the `TrackingEnforcer` (the #44 owner) -- a tracking mount is refused
  with the mode's reason (Astronomical: until guide-pulse Mount Align, S6.8b), never
  emergency-stopped. `enforce_after_unpark` runs #44's unpark enforcement after an automatic
  unpark (F12), exactly as the Mount panel does after its own.
- **Interruptible settle.** `settle()` waits on the executor's clock and ends at once on Stop.

Capabilities (S6.6b #55 C02): which primitives a mount has is read from
`MountCapabilities`; the Stop latch (`abort`/`clear_abort`) has no capability field (the field
set is frozen), so this module is its single probe.

No Qt; no `time.sleep` (the injected `Clock`, #53).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from astrotool_core.mount.operating_mode import (
    TERRESTRIAL_TRACKING_REPAIR_FAILED,
    OperatingMode,
    TrackingEnforcer,
    TrackingGate,
    axis_move_gate_for,
)
from astrotool_core.mount.tracking_mode import WORKER_DECISION_FRESH_WAIT_S
from astrotool_core.timing import SYSTEM_CLOCK, Clock

from collimation_tool.application.mount_commands import rearm_after_stop

_log = logging.getLogger(__name__)

#: Prefix of every busy refusal, so callers and tests can recognise it.
MOTION_BUSY = "mount busy"


#: The status note shown after a Terrestrial move whose tracking had to be turned off first.
TERRESTRIAL_TRACKING_REPAIRED_NOTE = (
    "the mount was tracking in Terrestrial mode -- the tracking policy turned tracking off "
    "before this move"
)


@dataclass(frozen=True)
class AxisMoveDecision:
    """`axis_move_decision`'s answer: whether the axis move may run, why not, and a note to
    show when it may only because the Terrestrial tracking policy repaired tracking first."""

    allowed: bool
    reason: str
    note: str | None = None


@dataclass
class MotionClaim:
    """One accepted mount motion. `cancel` is set by `MountMotionExecutor.abort()` (and by the
    owner's own Stop); every wait of the motion takes it."""

    owner: str
    mount: object | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()


def mount_abort(mount: object | None) -> bool:
    """Send the mount adapter's lock-free Stop (S6.0c) if it has one; True when sent. The single
    probe for the Stop latch (no `MountCapabilities` field exists for it, see module docstring)."""
    abort = getattr(mount, "abort", None)
    if not callable(abort):
        return False
    abort()
    return True


class MountMotionExecutor:
    def __init__(
        self,
        mount: object | None = None,
        *,
        tracking_enforcer: TrackingEnforcer | None = None,
        clock: Clock | None = None,
    ) -> None:
        """`mount`: the shared RA/Dec motion adapter (`OnStepMountPulseAdapter` in production)
        whose Stop `abort()` sends -- also when no claim is in flight (Mount Align's Stop and
        quit). `tracking_enforcer`: the app-wide #44 policy (None = no mode policy: a tracking
        mount is still refused, with a neutral reason, and no unpark enforcement runs)."""
        self._mount = mount
        self._enforcer = tracking_enforcer
        self._clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        #: Guards `_claim`. Held around the Stop latch's own calls (`abort()` / `clear_abort()`:
        #: non-blocking flag + worker hand-off, no mount I/O) so a Stop and a new claim's re-arm
        #: are ordered; never held around a move, a status read or any other mount I/O.
        self._lock = threading.Lock()
        self._claim: MotionClaim | None = None
        #: S6.8c-R5: observers of `in_flight` (see `add_in_flight_listener`).
        self._listeners: list[Callable[[str | None], None]] = []

    @property
    def tracking_enforcer(self) -> TrackingEnforcer | None:
        return self._enforcer

    @property
    def in_flight(self) -> str | None:
        """The owner of the motion in flight, or None."""
        with self._lock:
            return None if self._claim is None else self._claim.owner

    def add_in_flight_listener(self, listener: Callable[[str | None], None]) -> None:
        """S6.8c-R5: call `listener(in_flight)` after every accepted claim and every release
        that ended the motion in flight -- on the claiming/releasing thread, outside the lock
        (a UI marshals it to its own thread and re-reads `in_flight`). A raising listener is
        logged, never propagated into the motion."""
        self._listeners.append(listener)

    def _notify(self, in_flight: str | None) -> None:
        for listener in list(self._listeners):
            try:
                listener(in_flight)
            except Exception:  # noqa: BLE001 -- an observer must never break a motion
                _log.exception("mount-motion in-flight listener failed")

    def claim(self, owner: str, mount: object | None = None) -> MotionClaim | str:
        """Start one motion for `owner` (a human-readable name, e.g. "Mount Align"): the claim,
        or -- while another motion is in flight -- the busy reason (nothing re-armed or sent).
        An accepted claim re-arms the Stop latch of `mount` (default: the executor's mount)."""
        target = mount if mount is not None else self._mount
        with self._lock:
            current = self._claim
            if current is not None:
                return (
                    f"{MOTION_BUSY} -- {current.owner} is moving the mount; {owner} was not "
                    "started (nothing was sent)"
                )
            claim = MotionClaim(owner, target)
            self._claim = claim
            rearm_after_stop(target)
            if self._mount is not None and self._mount is not target:
                rearm_after_stop(self._mount)
        self._notify(owner)
        return claim

    def release(self, claim: MotionClaim) -> None:
        """End `claim` (idempotent; a stale claim never releases a newer one)."""
        with self._lock:
            if self._claim is not claim:
                return
            self._claim = None
        self._notify(None)

    def abort(self, owner: str | None = None) -> None:
        """Stop (any thread, returns at once): cancel the motion in flight and send the mount's
        lock-free Stop -- to the claim's mount and the executor's own (each once). With `owner`
        (a workflow's own Cancel, e.g. fine collimation's) only that owner's motion is stopped:
        nothing happens while no motion or another workflow's motion is in flight. The
        decision and the cancel are taken atomically with `claim()`/`release()` (review: a
        stale Cancel must never latch into a motion another owner claimed meanwhile)."""
        mounts: list[object] = []
        with self._lock:
            claim = self._claim
            if owner is not None and (claim is None or claim.owner != owner):
                return
            if claim is not None:
                claim.cancel.set()
                if claim.mount is not None:
                    mounts.append(claim.mount)
            if self._mount is not None and all(m is not self._mount for m in mounts):
                mounts.append(self._mount)
            for mount in mounts:
                mount_abort(mount)

    def abort_in_flight(self) -> bool:
        """S6.8c-R5 (the global mount Stop): `abort()` with no owner, but only while a motion
        is in flight -- decided under the same lock as `claim()`/`release()`, so a motion
        released at the moment of the click never turns this into an idle emergency stop.
        False (nothing cancelled, nothing sent) while idle; True when the claim in flight was
        cancelled and the Stop sent to its mount and the executor's own (each once)."""
        mounts: list[object] = []
        with self._lock:
            claim = self._claim
            if claim is None:
                return False
            claim.cancel.set()
            if claim.mount is not None:
                mounts.append(claim.mount)
            if self._mount is not None and all(m is not self._mount for m in mounts):
                mounts.append(self._mount)
            for mount in mounts:
                mount_abort(mount)
        return True

    def settle(self, claim: MotionClaim | None, seconds: float) -> bool:
        """Wait `seconds` on the executor's clock; False when Stop ended it early."""
        if seconds <= 0:
            return claim is None or not claim.cancelled
        return bool(self._clock.sleep(seconds, claim.cancel if claim is not None else None))

    def axis_move_gate(self, tracking: bool | None, context: str) -> TrackingGate:
        """R1 (see the module docstring): may an axis GOTO start, given a FRESH tracking reading
        (None = unknown, fails closed)? Never sends anything."""
        if self._enforcer is not None:
            return self._enforcer.axis_move_gate(context, tracking=tracking)
        return axis_move_gate_for(None, tracking)

    def axis_move_decision(
        self,
        tracking: bool | None,
        context: str,
        *,
        reread: Callable[[], bool | None],
        cancel: threading.Event | None = None,
    ) -> AxisMoveDecision:
        """`axis_move_gate`, plus the one sanctioned repair (#44 user decision: Terrestrial =>
        tracking OFF, owned by the enforcer): a Terrestrial move refused because the mount
        tracks gets the enforcer's tracking-off (its own trail entry), then ONE fresh reading
        (`reread`) and one more gate check. Never in Astronomical mode (S6.8b), never silent:
        an allowed move carries a note; a refusal says how to recover."""
        gate = self.axis_move_gate(tracking, context)
        enforcer = self._enforcer
        if (
            gate.allowed
            or tracking is not True
            or enforcer is None
            or enforcer.mode is not OperatingMode.TERRESTRIAL
        ):
            return AxisMoveDecision(gate.allowed, gate.reason)
        enforcer.enforce(
            f"{context}:repair", fresh_wait_s=WORKER_DECISION_FRESH_WAIT_S, cancel=cancel
        )
        if cancel is not None and cancel.is_set():
            return AxisMoveDecision(False, "stopped by the user -- no move sent")
        again = self.axis_move_gate(reread(), context)
        if again.allowed:
            return AxisMoveDecision(True, again.reason, TERRESTRIAL_TRACKING_REPAIRED_NOTE)
        return AxisMoveDecision(False, TERRESTRIAL_TRACKING_REPAIR_FAILED)

    def enforce_after_unpark(self, cancel: threading.Event | None = None) -> TrackingGate | None:
        """F12: #44's unpark enforcement after an automatic unpark (worker thread: waits for a
        fresh reading like the other worker decisions); None without a policy."""
        if self._enforcer is None:
            return None
        return self._enforcer.enforce(
            "unpark", fresh_wait_s=WORKER_DECISION_FRESH_WAIT_S, cancel=cancel
        )
