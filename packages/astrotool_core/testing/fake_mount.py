"""Mount test doubles.

`FakeMountAdapter` -- a `MountPort` that accepts and records TIMED pulses (`pulse_axis`) in
``pulse_log``. S6.8d (R9): only the dormant GuideTool (`GuideCorrectionPolicy`,
`calibrate_axes`) still uses timed pulses; the production OnStep adapter has none, so Mount Align
cannot move this fake (it reports no angular moves) -- Mount Align tests use
`FakeAngularMountAdapter`, the production contract (angular moves + guide pulses).
"""

from __future__ import annotations

from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    GuidePulseResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)

_FAKE_MOUNT_CAPABILITIES = MountCapabilities(
    supports_pulse_guiding=True,
    min_pulse_ms=1,
    max_pulse_ms=9999,
)


#: S6.8d (R9): the production contract -- `OnStepMountPulseAdapter` on OnStepAdapter 0.5.0:
#: angular moves and guide pulses while tracking, NO timed `pulse_axis`.
_FAKE_ANGULAR_MOUNT_CAPABILITIES = MountCapabilities(
    supports_pulse_guiding=False,
    min_pulse_ms=0,
    max_pulse_ms=0,
    supports_guide_pulses_while_tracking=True,
    supports_angular_moves=True,
)
#: The (min, max) guide-pulse duration the fake accepts (OnStepAdapter 0.5.0's own bounds).
FAKE_GUIDE_PULSE_RANGE_MS = (1, 5000)
#: Sidereal rate, arcsec per second -- the fake's default guide rate (1x sidereal).
_SIDEREAL_ARCSEC_PER_S = 15.041
_NO_TIMED_PULSE = "fake production mount: no timed pulse primitive (pulse_axis always refuses)"


class FakeMountAdapter:
    def __init__(self, *, fail_connect: bool = False, reject_first_n_pulses: int = 0) -> None:
        self._fail_connect = fail_connect
        self._connected = False
        self._tracking = False
        #: The first `reject_first_n_pulses` calls to pulse_axis() report
        #: accepted=False; every call after that accepts normally.
        self._reject_first_n_pulses = reject_first_n_pulses
        self._pulse_attempt_count = 0
        self.pulse_log: list[tuple[MountAxis, AxisDirection, int]] = []
        #: Parallel to pulse_log (one entry per pulse_axis call, same
        #: index) rather than folded into it -- pulse_log's 3-tuple shape
        #: is asserted on throughout the existing test suite, so keeping
        #: it unchanged and recording rate_preset separately avoids
        #: touching every one of those assertions for an unrelated field.
        self.rate_log: list[str | None] = []
        #: Every `abort()` call (the motion executor's Stop reaches the mount through it).
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
    """A `MountPort` with the PRODUCTION contract (S6.8d, R9): `AngularMotionPort.move_angular`
    and `GuidePulsePort.guide_pulse`, and -- like `OnStepMountPulseAdapter` -- a `pulse_axis`
    that always refuses. A small physical model: an angular move of `arcsec` moves the sky by
    `arcsec x scale` (`scale` per axis direction models a mount that does not move exactly what
    was commanded; 1.0 = exact), a guide pulse by `guide_rate x duration`. `sky_arcsec[axis]` is
    the cumulative signed result (axis1 + = east, axis2 + = north)."""

    def __init__(
        self,
        *,
        scale: dict[tuple[MountAxis, AxisDirection], float] | float = 1.0,
        guide_rate_arcsec_per_s: float = _SIDEREAL_ARCSEC_PER_S,
        refuse_angular: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._scale = scale
        self._guide_rate = guide_rate_arcsec_per_s
        self.refuse_angular = refuse_angular
        self.angular_log: list[tuple[MountAxis, AxisDirection, float]] = []
        self.guide_log: list[tuple[MountAxis, AxisDirection, int]] = []
        self.sky_arcsec: dict[MountAxis, float] = {MountAxis.AXIS1: 0.0, MountAxis.AXIS2: 0.0}

    def _scale_for(self, axis: MountAxis, direction: AxisDirection) -> float:
        if isinstance(self._scale, dict):
            return self._scale[(axis, direction)]
        return self._scale

    @staticmethod
    def _sign(direction: AxisDirection) -> int:
        return 1 if direction is AxisDirection.POSITIVE else -1

    def capabilities(self) -> MountCapabilities:
        """The production capabilities (no angular floor, no stated maximum)."""
        return _FAKE_ANGULAR_MOUNT_CAPABILITIES

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        """Refuses, exactly like the production adapter (no timed primitive)."""
        return CommandResult(accepted=False, message=_NO_TIMED_PULSE)

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        if not self._connected:
            return CommandResult(accepted=False, message="not connected")
        if self.refuse_angular is not None:
            return CommandResult(accepted=False, message=self.refuse_angular)
        self.angular_log.append((axis, direction, arcsec))
        self.sky_arcsec[axis] += self._sign(direction) * arcsec * self._scale_for(axis, direction)
        return CommandResult(accepted=True)

    @property
    def guide_pulse_range_ms(self) -> tuple[int, int] | None:
        return FAKE_GUIDE_PULSE_RANGE_MS

    def guide_pulse(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int
    ) -> GuidePulseResult:
        if not self._connected:
            return GuidePulseResult(accepted=False, message="not connected")
        low, high = FAKE_GUIDE_PULSE_RANGE_MS
        if not low <= duration_ms <= high:
            return GuidePulseResult(
                accepted=False, message=f"guide pulse of {duration_ms} ms outside {low}..{high}"
            )
        self.guide_log.append((axis, direction, duration_ms))
        self.sky_arcsec[axis] += self._sign(direction) * self._guide_rate * duration_ms / 1000.0
        return GuidePulseResult(
            accepted=True,
            sent=True,
            tracking_preserved=True,
            chunks_requested=1,
            chunks_completed=1,
        )


class FakeEquivalentMsMountAdapter(FakeAngularMountAdapter):
    """`FakeAngularMountAdapter` that also records every accepted angular move in
    `equivalent_ms_log` as (axis, direction, equivalent ms) -- the S6.0 calibration unit Mount
    Align sizes in (`unit_arcsec_per_s`, default the configured `calibration_center_rate_x`
    x sidereal). S6.8d: lets the panel tests that model image motion per ms keep doing so on
    the production contract (they used to read the timed `pulse_log`)."""

    def __init__(self, *, unit_arcsec_per_s: float | None = None, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        if unit_arcsec_per_s is None:
            from astrotool_core.config import MountAlignmentSettings

            unit_arcsec_per_s = (
                MountAlignmentSettings().calibration_center_rate_x * _SIDEREAL_ARCSEC_PER_S
            )
        self._unit_arcsec_per_s = unit_arcsec_per_s
        self.equivalent_ms_log: list[tuple[MountAxis, AxisDirection, int]] = []

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        result = super().move_angular(axis, direction, arcsec)
        if result.accepted:
            ms = round(arcsec / self._unit_arcsec_per_s * 1000.0)
            self.equivalent_ms_log.append((axis, direction, ms))
        return result
