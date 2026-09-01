"""FakeMountAdapter — MountPort test double that accepts and records pulses.

Named explicitly in collimation-guidetool-architektur.md alongside
NoMountAdapter and IndiMountAdapter. Records every accepted pulse in
``pulse_log`` so calibration/guide-controller tests can assert what was
actually sent, without a real INDI/OnStep connection.
"""

from __future__ import annotations

from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)

_FAKE_MOUNT_CAPABILITIES = MountCapabilities(
    supports_pulse_guiding=True,
    min_pulse_ms=1,
    max_pulse_ms=9999,
)


class FakeMountAdapter:
    def __init__(
        self, *, fail_connect: bool = False, reject_first_n_pulses: int = 0
    ) -> None:
        self._fail_connect = fail_connect
        self._connected = False
        self._tracking = False
        #: Real report 45e5ae86 ("SEV 1"): a pulse can still get rejected
        #: for a short window even after the mount has already confirmed
        #: unparked -- see IndiMountPulseAdapter's own module docstring
        #: for the sourced mechanism, and MountTestMoveRunner's own retry
        #: logic this simulates for. The first `reject_first_n_pulses`
        #: calls to pulse_axis() report accepted=False; every call after
        #: that accepts normally.
        self._reject_first_n_pulses = reject_first_n_pulses
        self._pulse_attempt_count = 0
        self.pulse_log: list[tuple[MountAxis, AxisDirection, int]] = []
        #: Parallel to pulse_log (one entry per pulse_axis call, same
        #: index) rather than folded into it -- pulse_log's 3-tuple shape
        #: is asserted on throughout the existing test suite, so keeping
        #: it unchanged and recording rate_preset separately avoids
        #: touching every one of those assertions for an unrelated field.
        self.rate_log: list[str | None] = []
        #: Not part of MountPort (see IndiMountPulseAdapter.abort()'s own
        #: docstring on why) -- present here purely so
        #: MountTestMovePanel's duck-typed "Stop" button is testable
        #: against a fake instead of only the real INDI adapter.
        self.abort_log: list[None] = []

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("FakeMountAdapter: connect failed (simulated)")
        self._connected = True
        self._tracking = True

    def disconnect(self) -> None:
        self._connected = False
        self._tracking = False

    def capabilities(self) -> MountCapabilities:
        return _FAKE_MOUNT_CAPABILITIES

    def status(self) -> MountStatus:
        return MountStatus(connected=self._connected, tracking=self._tracking, slewing=False)

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        if not self._connected:
            return CommandResult(accepted=False, message="not connected")
        self._pulse_attempt_count += 1
        if self._pulse_attempt_count <= self._reject_first_n_pulses:
            return CommandResult(
                accepted=False, message="mount rejected the motion command -- still parked?"
            )
        self.pulse_log.append((axis, direction, duration_ms))
        self.rate_log.append(rate_preset)
        return CommandResult(accepted=True)

    def abort(self) -> None:
        self.abort_log.append(None)
