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

import threading
from dataclasses import dataclass, field

from astrotool_core.mount.operating_mode import TrackingEnforcer, TrackingGate, axis_move_gate_for
from astrotool_core.mount.tracking_mode import WORKER_DECISION_FRESH_WAIT_S
from astrotool_core.timing import SYSTEM_CLOCK, Clock

from collimation_tool.application.mount_commands import rearm_after_stop

#: Prefix of every busy refusal, so callers and tests can recognise it.
MOTION_BUSY = "mount busy"


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
        #: Guards `_claim` only; never held while talking to the mount.
        self._lock = threading.Lock()
        self._claim: MotionClaim | None = None

    @property
    def tracking_enforcer(self) -> TrackingEnforcer | None:
        return self._enforcer

    @property
    def in_flight(self) -> str | None:
        """The owner of the motion in flight, or None."""
        with self._lock:
            return None if self._claim is None else self._claim.owner

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
        return claim

    def release(self, claim: MotionClaim) -> None:
        """End `claim` (idempotent; a stale claim never releases a newer one)."""
        with self._lock:
            if self._claim is claim:
                self._claim = None

    def abort(self, owner: str | None = None) -> None:
        """Stop (any thread, returns at once): cancel the motion in flight and send the mount's
        lock-free Stop -- to the claim's mount and the executor's own (each once). With `owner`
        (a workflow's own Cancel, e.g. fine collimation's) only that owner's motion is stopped:
        nothing happens while no motion or another workflow's motion is in flight."""
        with self._lock:
            claim = self._claim
        if owner is not None and (claim is None or claim.owner != owner):
            return
        mounts: list[object] = []
        if claim is not None:
            claim.cancel.set()
            if claim.mount is not None:
                mounts.append(claim.mount)
        if self._mount is not None and all(m is not self._mount for m in mounts):
            mounts.append(self._mount)
        for mount in mounts:
            mount_abort(mount)

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

    def enforce_after_unpark(self, cancel: threading.Event | None = None) -> TrackingGate | None:
        """F12: #44's unpark enforcement after an automatic unpark (worker thread: waits for a
        fresh reading like the other worker decisions); None without a policy."""
        if self._enforcer is None:
            return None
        return self._enforcer.enforce(
            "unpark", fresh_wait_s=WORKER_DECISION_FRESH_WAIT_S, cancel=cancel
        )
