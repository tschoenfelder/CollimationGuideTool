"""Operating mode and the ONE mode-aware mount-tracking policy (issue #44).

    TERRESTRIAL   -> mount tracking is REQUIRED OFF (tracking would add
                     continuous image motion against a fixed scene, corrupting
                     calibration, registration, autofocus and collimation).
    ASTRONOMICAL  -> tracking is workflow-dependent; this policy never forces it.

Consumers (mount align, FOV registration, autofocus, collimation) ask the
`TrackingEnforcer` instead of each deciding tracking state themselves. It never
turns tracking ON, always verifies the result, and fails CLOSED with an
explicit reason if tracking cannot be disabled. Every check is recorded in a
bounded trail so a field failure shows whether tracking was ON at any point a
measurement was allowed.

Issue #48 / S6.4: the global mode is the ONLY Terrestrial/Astronomical source. Workflows
derive their tracking need from it (`measurement_tracking`), and a tracking-ON request while
the mode is TERRESTRIAL is refused here (fail closed, nothing sent) -- a request snapshotted
before a mode switch can never re-enable tracking. S6.0c review P-a: when entering (or
enforcing) TERRESTRIAL found the mount busy, the gate stays closed and a one-shot re-enforce
is left pending; the owner of the mount status poll calls `retry_pending` and the enforcer
turns tracking off as soon as a fresh reading exists again (no waiting, no new sleeps).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.mount.tracking_mode import (
    MOUNT_BUSY_REASON,
    TrackingMode,
    TrackingVerificationResult,
    TrackingVerificationStatus,
    ensure_tracking_mode,
)
from astrotool_core.timing import Clock

#: OnStep's INDI driver can take a couple of seconds to reflect a TRACK_OFF.
_DEFAULT_SETTLE_TIMEOUT_S = 3.0
_TRAIL_LENGTH = 100

#: #48: why a tracking-ON request is refused while the global mode is Terrestrial.
TERRESTRIAL_TRACKING_ON_REFUSED_REASON = (
    "terrestrial operating mode: mount tracking must stay off -- tracking-on request refused "
    "(switch the operating mode to Astronomical for star measurements)"
)


class OperatingMode(Enum):
    TERRESTRIAL = "terrestrial"
    ASTRONOMICAL = "astronomical"


@dataclass(frozen=True)
class TrackingGate:
    """Whether a measurement may proceed, and why."""

    allowed: bool
    reason: str


class TrackingEnforcer:
    def __init__(
        self,
        mount_park: MountParkPort,
        mode: OperatingMode = OperatingMode.TERRESTRIAL,
        *,
        settle_timeout_s: float = _DEFAULT_SETTLE_TIMEOUT_S,
        clock: Clock | None = None,
    ) -> None:
        self._mount = mount_park
        self._mode = mode
        self._settle_timeout_s = settle_timeout_s
        #: Issue #53 (S3a dependency): the settle poll of every repair reads/waits through it.
        self._clock = clock
        #: S6.0c review P-a: a TERRESTRIAL enforcement found the mount busy and is still owed.
        self._reenforce_pending = False
        #: S6.4 round 3: count of tracking-ON checks overtaken by a switch to TERRESTRIAL.
        self._on_conflicts = 0
        #: Guards the decision state (_mode, _reenforce_pending, _last_gate, _on_conflicts) and
        #: the trail. Never held across mount I/O, so the GUI thread never waits behind a worker.
        self._lock = threading.Lock()
        self._trail: deque[dict[str, Any]] = deque(maxlen=_TRAIL_LENGTH)
        self._last_gate = TrackingGate(True, "no check yet")

    @property
    def mode(self) -> OperatingMode:
        return self._mode

    @staticmethod
    def required_tracking(mode: OperatingMode) -> TrackingMode | None:
        """The tracking state a mode REQUIRES (None = not forced by policy)."""
        return TrackingMode.OFF if mode is OperatingMode.TERRESTRIAL else None

    def measurement_tracking(self) -> TrackingMode:
        """#48: the tracking a mount-motion MEASUREMENT (Mount Align) needs in the current
        mode -- OFF against a fixed terrestrial scene, ON for star measurements (the sky must
        stay put while the mount is moved). Derived from the global mode only."""
        return TrackingMode.OFF if self._mode is OperatingMode.TERRESTRIAL else TrackingMode.ON

    def set_mode(self, mode: OperatingMode) -> TrackingGate:
        """Switch mode. Entering TERRESTRIAL forces tracking OFF (when a mount
        is available; otherwise the intent is kept and enforced on connect).

        Only the tracking policy lives here. Cross-workflow rules on the mode -- user decision
        A (2026-10-06): an artificial-star target implies TERRESTRIAL, and choosing
        ASTRONOMICAL returns the target to a natural star -- live in MainWindow's mode wiring,
        the only production caller; a direct `set_mode(ASTRONOMICAL)` bypasses that rule."""
        with self._lock:
            self._mode = mode
            if mode is not OperatingMode.TERRESTRIAL:
                self._reenforce_pending = False  # astronomical never forces tracking
        return self.enforce(f"mode_change:{mode.value}")

    @property
    def reenforce_pending(self) -> bool:
        """S6.0c review P-a: a TERRESTRIAL enforcement was denied because the mount was busy
        and has not been carried out since."""
        return self._reenforce_pending

    def retry_pending(self, context: str) -> TrackingGate | None:
        """S6.0c review P-a: the one-shot re-enforce once the busy period ends. Cheap and
        non-blocking (meant for a GUI status poll): None -- nothing sent, nothing recorded --
        while nothing is pending or the mount still serves no fresh reading; otherwise the
        result of one `enforce(context)` (still pending only if that, too, found it busy)."""
        with self._lock:
            if not self._reenforce_pending or self._mode is not OperatingMode.TERRESTRIAL:
                self._reenforce_pending = False
                return None
        try:
            fresh = self._mount.status().fresh
        except Exception as exc:  # noqa: BLE001 -- see verify(): fail closed, stop retrying
            return self._settle(
                TrackingGate(False, f"tracking off could not be established -- {exc}"),
                pending=False,
            )
        if not fresh:
            return None
        return self.enforce(context)

    def measurement_allowed(self) -> bool:
        return self._last_gate.allowed

    @property
    def last_gate(self) -> TrackingGate:
        return self._last_gate

    def enforce(self, context: str, *, fresh_wait_s: float = 0.0) -> TrackingGate:
        """Check (and, in TERRESTRIAL mode, repair) tracking for `context`. `fresh_wait_s`:
        0 on the GUI thread; off it, see `ensure_tracking_mode` (S6.0c re-review R1)."""
        required = self.required_tracking(self._mode)
        if required is None:
            gate = TrackingGate(True, "astronomical mode: tracking is workflow-dependent")
            self._record(context, before=self._tracking_now(), command=None, after=None, gate=gate)
            return self._remember(gate)
        return self.verify(required, context, fresh_wait_s=fresh_wait_s)

    def verify(
        self, required: TrackingMode, context: str, *, fresh_wait_s: float = 0.0
    ) -> TrackingGate:
        """Verify `required` tracking for `context` (used by callers whose
        workflow-specific requirement is stricter than the mode policy). #48: tracking ON is
        never established while the mode is TERRESTRIAL -- refused, nothing sent."""
        with self._lock:
            refused = required is TrackingMode.ON and self._mode is OperatingMode.TERRESTRIAL
            #: S6.4 round 3: a tracking-ON conflict recorded after this point means another
            #: thread's request may have re-enabled tracking behind this check.
            conflicts_at_start = self._on_conflicts
        if refused:
            gate = self._settle(TrackingGate(False, TERRESTRIAL_TRACKING_ON_REFUSED_REASON))
            self._record(context, before=self._tracking_now(), command=None, after=None, gate=gate)
            return gate
        before = self._tracking_now()
        try:
            result = ensure_tracking_mode(
                self._mount,
                required,
                settle_timeout_s=self._settle_timeout_s,
                clock=self._clock,
                fresh_wait_s=fresh_wait_s,
            )
        except Exception as exc:  # noqa: BLE001 -- a device error is a closed gate, never a crash
            # S6.4 review: e.g. OnStep's stop_tracking (an emergency stop) raising when the
            # driver never confirms it (OnStepAdapter#17), or a status read raising. Fail
            # CLOSED with the reason and drop any owed re-enforce -- a periodic retry must not
            # resend the command every tick.
            failed = self._settle(
                TrackingGate(
                    False, f"tracking {required.value} could not be established -- {exc}"
                ),
                pending=False,
            )
            self._record(
                context,
                before=before,
                command="stop_tracking" if required is TrackingMode.OFF else "start_tracking",
                after=self._tracking_now(),
                gate=failed,
            )
            return failed
        gate = self._settle_result(result, required, conflicts_at_start)
        command = None
        if result.status in (
            TrackingVerificationStatus.REPAIRED,
            TrackingVerificationStatus.REPAIR_FAILED,
        ):
            command = "stop_tracking" if required is TrackingMode.OFF else "start_tracking"
        self._record(
            context, before=before, command=command, after=self._tracking_now(), gate=gate
        )
        return gate

    def evidence(self) -> dict[str, Any]:
        with self._lock:
            return {
                "operating_mode": self._mode.value,
                "measurement_allowed": self._last_gate.allowed,
                "last_reason": self._last_gate.reason,
                "transitions": list(self._trail),
            }

    # ---- internals
    def _tracking_now(self) -> bool | None:
        """For the evidence trail: None unless a FRESH reading exists (S6.0c -- a held-over
        value is never recorded as the mount's state). Evidence only: a raising read is
        recorded as None, never raised (the decision read itself fails closed in verify)."""
        try:
            status = self._mount.status()
        except Exception:  # noqa: BLE001 -- evidence read; the decision path fails closed
            return None
        return status.tracking if status.available and status.fresh else None

    def _settle_result(
        self, result: TrackingVerificationResult, required: TrackingMode, conflicts_at_start: int
    ) -> TrackingGate:
        """Decide and store the gate atomically with the pending flag (S6.4 round 3: a worker's
        tracking-ON check and the GUI's tracking-OFF check may finish in either order)."""
        with self._lock:
            terrestrial = self._mode is OperatingMode.TERRESTRIAL
            if required is TrackingMode.ON and terrestrial:
                # The mode switched to TERRESTRIAL while this tracking-ON check ran
                # (check-then-act) -- never allowed; owe a re-enforce (tracking OFF).
                self._on_conflicts += 1
                self._reenforce_pending = True
                gate = TrackingGate(False, TERRESTRIAL_TRACKING_ON_REFUSED_REASON)
            elif required is TrackingMode.OFF and self._on_conflicts != conflicts_at_start:
                # A tracking-ON request overtook this check (it may have started tracking
                # after our reading): "off verified" would be stale -- stay closed, re-enforce.
                self._reenforce_pending = True
                gate = TrackingGate(
                    False,
                    "tracking may have been re-enabled by a concurrent request -- "
                    "terrestrial measurement blocked until it is turned off again",
                )
            else:
                if terrestrial and required is TrackingMode.OFF:
                    # P-a: a busy mount leaves the enforcement owed; any real answer settles it.
                    self._reenforce_pending = result.status is TrackingVerificationStatus.BUSY
                gate = self._gate_for(result, required)
            self._last_gate = gate
            return gate

    def _settle(self, gate: TrackingGate, *, pending: bool | None = None) -> TrackingGate:
        with self._lock:
            if pending is not None:
                self._reenforce_pending = pending
            self._last_gate = gate
            return gate

    def _gate_for(self, result: TrackingVerificationResult, required: TrackingMode) -> TrackingGate:
        status = result.status
        if status is TrackingVerificationStatus.BUSY:  # S6.0c: fail CLOSED, never "verified"
            return TrackingGate(False, MOUNT_BUSY_REASON)
        if status is TrackingVerificationStatus.UNAVAILABLE:
            return TrackingGate(True, "mount unavailable: no tracking to enforce")
        if result.ok:
            return TrackingGate(True, f"tracking {required.value} verified ({status.value})")
        if required is TrackingMode.OFF:
            return TrackingGate(
                False,
                "tracking could not be disabled -- terrestrial measurement blocked "
                "(the scene would move relative to the camera)",
            )
        return TrackingGate(False, "tracking could not be enabled -- measurement blocked")

    def _remember(self, gate: TrackingGate) -> TrackingGate:
        return self._settle(gate)

    def _record(
        self,
        context: str,
        *,
        before: bool | None,
        command: str | None,
        after: bool | None,
        gate: TrackingGate,
    ) -> None:
        with self._lock:
            self._trail.append(
                {
                    "at": time.time(),
                    "operating_mode": self._mode.value,
                    "context": context,
                    "tracking_before": before,
                    "command": command,
                    "tracking_after": after,
                    "allowed": gate.allowed,
                    "reason": gate.reason,
                }
            )
