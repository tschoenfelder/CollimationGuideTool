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
    TrackingMode,
    TrackingVerificationResult,
    TrackingVerificationStatus,
    ensure_tracking_mode,
)

#: OnStep's INDI driver can take a couple of seconds to reflect a TRACK_OFF.
_DEFAULT_SETTLE_TIMEOUT_S = 3.0
_TRAIL_LENGTH = 100


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
    ) -> None:
        self._mount = mount_park
        self._mode = mode
        self._settle_timeout_s = settle_timeout_s
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

    def set_mode(self, mode: OperatingMode) -> TrackingGate:
        """Switch mode. Entering TERRESTRIAL forces tracking OFF (when a mount
        is available; otherwise the intent is kept and enforced on connect)."""
        self._mode = mode
        return self.enforce(f"mode_change:{mode.value}")

    def measurement_allowed(self) -> bool:
        return self._last_gate.allowed

    @property
    def last_gate(self) -> TrackingGate:
        return self._last_gate

    def enforce(self, context: str) -> TrackingGate:
        """Check (and, in TERRESTRIAL mode, repair) tracking for `context`."""
        required = self.required_tracking(self._mode)
        if required is None:
            gate = TrackingGate(True, "astronomical mode: tracking is workflow-dependent")
            self._record(context, before=self._tracking_now(), command=None, after=None, gate=gate)
            return self._remember(gate)
        return self.verify(required, context)

    def verify(self, required: TrackingMode, context: str) -> TrackingGate:
        """Verify `required` tracking for `context` (used by callers whose
        workflow-specific requirement is stricter than the mode policy)."""
        before = self._tracking_now()
        result = ensure_tracking_mode(
            self._mount, required, settle_timeout_s=self._settle_timeout_s
        )
        gate = self._gate_for(result, required)
        command = None
        if result.status in (
            TrackingVerificationStatus.REPAIRED,
            TrackingVerificationStatus.REPAIR_FAILED,
        ):
            command = "stop_tracking" if required is TrackingMode.OFF else "start_tracking"
        self._record(
            context, before=before, command=command, after=self._tracking_now(), gate=gate
        )
        return self._remember(gate)

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
        status = self._mount.status()
        return status.tracking if status.available else None

    def _gate_for(self, result: TrackingVerificationResult, required: TrackingMode) -> TrackingGate:
        status = result.status
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
        self._last_gate = gate
        return gate

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
