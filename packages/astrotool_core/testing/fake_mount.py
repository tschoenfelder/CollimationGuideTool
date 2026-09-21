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
    def __init__(self, *, fail_connect: bool = False, reject_first_n_pulses: int = 0) -> None:
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


class FakeAngularMountAdapter(FakeMountAdapter):
    """`FakeMountAdapter` + `AngularMotionPort`, with a small physical model.

    The mount really moves the sky at `true_rate` arcsec/s per axis direction (unknown
    to the application). A timed pulse moves `true_rate x duration`; an angular move of
    `arcsec` runs for `arcsec / installed_rate` seconds (that is what OnStepAdapter does),
    so it moves `arcsec x true_rate / installed_rate` -- exactly right only when the
    installed rate was measured correctly. `sky_arcsec[axis]` is the cumulative signed
    result (axis1 + = east, axis2 + = north)."""

    def __init__(
        self,
        *,
        true_rate: dict[tuple[MountAxis, AxisDirection], float] | float = 100.0,
        refuse_angular: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._true_rate = true_rate
        self._installed: dict[tuple[MountAxis, AxisDirection], float] = {}
        self.refuse_angular = refuse_angular
        self.angular_log: list[tuple[MountAxis, AxisDirection, float]] = []
        self.install_log: list[tuple[MountAxis, AxisDirection, float]] = []
        self.sky_arcsec: dict[MountAxis, float] = {MountAxis.AXIS1: 0.0, MountAxis.AXIS2: 0.0}

    def _true(self, axis: MountAxis, direction: AxisDirection) -> float:
        if isinstance(self._true_rate, dict):
            return self._true_rate[(axis, direction)]
        return self._true_rate

    @staticmethod
    def _sign(direction: AxisDirection) -> int:
        return 1 if direction is AxisDirection.POSITIVE else -1

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        result = super().pulse_axis(axis, direction, duration_ms, rate_preset=rate_preset)
        if result.accepted:
            self.sky_arcsec[axis] += (
                self._sign(direction) * self._true(axis, direction) * duration_ms / 1000.0
            )
        return result

    def installed_rate(self, axis: MountAxis, direction: AxisDirection) -> float | None:
        return self._installed.get((axis, direction))

    def install_rate(self, axis: MountAxis, direction: AxisDirection, arcsec_per_s: float) -> None:
        self._installed[(axis, direction)] = arcsec_per_s
        self.install_log.append((axis, direction, arcsec_per_s))

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        if not self._connected:
            return CommandResult(accepted=False, message="not connected")
        if self.refuse_angular is not None:
            return CommandResult(accepted=False, message=self.refuse_angular)
        installed = self._installed.get((axis, direction))
        if installed is None:
            return CommandResult(accepted=False, message="no centering rate installed")
        self.angular_log.append((axis, direction, arcsec))
        self.sky_arcsec[axis] += (
            self._sign(direction) * arcsec * self._true(axis, direction) / installed
        )
        return CommandResult(accepted=True)
