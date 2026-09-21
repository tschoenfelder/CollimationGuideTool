"""OnStepMountPulseAdapter — `MountPort` over OnStepAdapter's bounded timed motion.

Raw protocol sequencing (rate selection, motion start/stop, safety polling,
timing) stays inside OnStepAdapter (`move_ra_timed`/`move_dec_timed`); this
shim only maps axis/direction and the rate preset, and reports the adapter's
verdict. The mode follows the tracking state, as OnStepAdapter requires:
tracking OFF (terrestrial) -> `manual` (allowed at home), tracking ON ->
`center`.
"""

from __future__ import annotations

import threading
from typing import Literal

from onstep_adapter import OnStepMount, OnStepSafetyError
from onstep_adapter.ports.mount import MountState

from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.onstep.connection import OnStepConnection

_MIN_PULSE_MS = 20
_MAX_PULSE_MS = 120000

_RA: dict[AxisDirection, Literal["east", "west"]] = {
    AxisDirection.POSITIVE: "east",
    AxisDirection.NEGATIVE: "west",
}
_DEC: dict[AxisDirection, Literal["north", "south"]] = {
    AxisDirection.POSITIVE: "north",
    AxisDirection.NEGATIVE: "south",
}


class OnStepMountPulseAdapter:
    def __init__(self, connection: OnStepConnection) -> None:
        self._connection = connection
        self._held = False
        self._cancel = threading.Event()

    def connect(self) -> None:
        if not self._held:
            self._connection.acquire()
            self._held = True

    def disconnect(self) -> None:
        if self._held:
            self._held = False
            self._connection.release()

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            supports_pulse_guiding=True, min_pulse_ms=_MIN_PULSE_MS, max_pulse_ms=_MAX_PULSE_MS
        )

    def _mount(self) -> OnStepMount | None:
        client = self._connection.client
        return client.mount if self._held and client is not None else None

    def status(self) -> MountStatus:
        mount = self._mount()
        if mount is None:
            return MountStatus(connected=False, tracking=False, slewing=False)
        state = mount.get_state()
        return MountStatus(
            connected=True, tracking=state == MountState.TRACKING, slewing=mount.is_slewing()
        )

    def abort(self) -> None:
        """Cancel a running pulse: cooperative cancel first, then OnStep's own stop."""
        self._cancel.set()
        mount = self._mount()
        if mount is not None:
            mount.stop()

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        mount = self._mount()
        if mount is None:
            return CommandResult(accepted=False, message="not connected")
        try:
            preset = None if rate_preset is None else int(rate_preset)
        except ValueError:
            return CommandResult(accepted=False, message=f"invalid rate preset {rate_preset!r}")
        self._cancel.clear()
        mode: Literal["center", "manual"] = (
            "center" if mount.get_state() == MountState.TRACKING else "manual"
        )
        try:
            if axis == MountAxis.AXIS1:
                result = mount.move_ra_timed(
                    _RA[direction],
                    duration_ms,
                    mode=mode,
                    rate_preset=preset,
                    cancel_check=self._cancel.is_set,
                )
            else:
                result = mount.move_dec_timed(
                    _DEC[direction],
                    duration_ms,
                    mode=mode,
                    rate_preset=preset,
                    cancel_check=self._cancel.is_set,
                )
        except OnStepSafetyError as exc:
            return CommandResult(accepted=False, message=f"OnStepAdapter refused the move: {exc}")
        except ValueError as exc:
            return CommandResult(accepted=False, message=str(exc))
        if result.cancelled:
            return CommandResult(accepted=False, message="pulse cancelled")
        if not result.ok:
            return CommandResult(
                accepted=False, message=result.error or "OnStepAdapter reported failure"
            )
        return CommandResult(accepted=True)
