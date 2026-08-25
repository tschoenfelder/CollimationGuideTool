"""IndiMountAdapter — MountPort implementation wrapping onstep-adapter.

Named ``indi_adapter.py`` to match the architecture doc's file name even
though it speaks OnStep's native serial (LX200-derived) protocol directly
rather than the INDI wire protocol — see PLAN.md's "Mount protocol"
decision. All actual serial/protocol logic lives in the pip-installed
``onstep-adapter`` package; this file contains only the MountPort shim.
Never edit onstep_adapter internals from this repo — flag gaps and wait
(see CONTRIBUTING.md and README.md).
"""

from __future__ import annotations

from onstep_adapter.client import OnStepClient
from onstep_adapter.ports.mount import MountState as OnStepMountState

from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)

_MIN_PULSE_MS = 1
_MAX_PULSE_MS = 9999

# AXIS1 (RA/azimuth) <-> e/w, AXIS2 (Dec/altitude) <-> n/s — onstep_adapter's
# guide() direction codes.
_DIRECTION_CODE: dict[tuple[MountAxis, AxisDirection], str] = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): "e",
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): "w",
    (MountAxis.AXIS2, AxisDirection.POSITIVE): "n",
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): "s",
}


class IndiMountAdapter:
    """MountPort backed by a real OnStep mount over serial."""

    def __init__(self, port: str, *, baud_rate: int = 9600, timeout: float = 2.0) -> None:
        self._client = OnStepClient(port, baud_rate=baud_rate, timeout=timeout)
        self._connected = False

    def connect(self) -> None:  # pragma: no cover — requires a real OnStep mount
        result = self._client.connect()
        if not result.mount_connected:
            raise ConnectionError(
                f"IndiMountAdapter: could not connect to OnStep on {self._client.port}"
            )
        self._connected = True

    def disconnect(self) -> None:  # pragma: no cover — requires a real OnStep mount
        self._client.mount.disconnect()
        self._client.close()
        self._connected = False

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            supports_pulse_guiding=True,
            min_pulse_ms=_MIN_PULSE_MS,
            max_pulse_ms=_MAX_PULSE_MS,
        )

    def status(self) -> MountStatus:
        if not self._connected:
            return MountStatus(connected=False, tracking=False, slewing=False)
        return self._status_connected()  # pragma: no cover — requires a real OnStep mount

    def _status_connected(self) -> MountStatus:  # pragma: no cover
        state = self._client.mount.get_state()
        return MountStatus(
            connected=True,
            tracking=state == OnStepMountState.TRACKING,
            slewing=self._client.mount.is_slewing(),
        )

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
    ) -> CommandResult:
        if not self._connected:
            return CommandResult(accepted=False, message="not connected")
        return self._pulse_axis_connected(axis, direction, duration_ms)  # pragma: no cover

    def _pulse_axis_connected(  # pragma: no cover — requires a real OnStep mount
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
    ) -> CommandResult:
        direction_code = _DIRECTION_CODE[(axis, direction)]
        clamped_ms = max(_MIN_PULSE_MS, min(_MAX_PULSE_MS, duration_ms))
        accepted = self._client.mount.guide(direction_code, clamped_ms)
        if not accepted:
            return CommandResult(accepted=False, message="OnStep guide pulse rejected")
        return CommandResult(accepted=True)
