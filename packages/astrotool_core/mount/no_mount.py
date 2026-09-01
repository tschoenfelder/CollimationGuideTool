"""NoMountAdapter — MountPort stand-in for "no mount configured".

Named explicitly in collimation-guidetool-architektur.md alongside
FakeMountAdapter and IndiMountAdapter as one of the three implementations
every mount contract test must pass. Always reports disconnected/not
pulse-capable; never accepts a pulse.
"""

from __future__ import annotations

from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)

_NO_MOUNT_CAPABILITIES = MountCapabilities(
    supports_pulse_guiding=False,
    min_pulse_ms=0,
    max_pulse_ms=0,
)


class NoMountAdapter:
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capabilities(self) -> MountCapabilities:
        return _NO_MOUNT_CAPABILITIES

    def status(self) -> MountStatus:
        return MountStatus(connected=False, tracking=False, slewing=False)

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        return CommandResult(accepted=False, message="no mount configured")
