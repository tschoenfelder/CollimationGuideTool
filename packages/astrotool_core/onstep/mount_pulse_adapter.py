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

from onstep_adapter import (
    AxisMotionResult,
    OnStepMotionCalibration,
    OnStepMount,
    OnStepSafetyError,
)
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


#: `OnStepMotionCalibration` field per axis direction (centering rates: the rate
#: OnStepAdapter's center-mode moves and an un-preset timed move both run at).
_CENTER_FIELD: dict[tuple[MountAxis, AxisDirection], str] = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): "center_ra_east_arcsec_per_s",
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): "center_ra_west_arcsec_per_s",
    (MountAxis.AXIS2, AxisDirection.POSITIVE): "center_dec_north_arcsec_per_s",
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): "center_dec_south_arcsec_per_s",
}


class OnStepMountPulseAdapter:
    def __init__(self, connection: OnStepConnection) -> None:
        self._connection = connection
        self._held = False
        self._cancel = threading.Event()
        self._rates: dict[tuple[MountAxis, AxisDirection], float] = {}

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

    # ---- AngularMotionPort ------------------------------------------------
    def installed_rate(self, axis: MountAxis, direction: AxisDirection) -> float | None:
        return self._rates.get((axis, direction))

    def install_rate(self, axis: MountAxis, direction: AxisDirection, arcsec_per_s: float) -> None:
        """Install one measured centering rate into OnStepAdapter (partial calibration:
        the other directions keep whatever is already installed)."""
        if not (0.0 < arcsec_per_s < float("inf")):
            raise ValueError(f"invalid centering rate {arcsec_per_s!r}")
        mount = self._mount()
        if mount is None:
            raise ConnectionError("OnStep mount is not connected")
        self._rates[(axis, direction)] = float(arcsec_per_s)
        fields = {_CENTER_FIELD[key]: rate for key, rate in self._rates.items()}
        mount.set_motion_calibration(OnStepMotionCalibration(**fields))

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        mount = self._mount()
        if mount is None:
            return CommandResult(accepted=False, message="not connected")
        if not (arcsec > 0.0):
            return CommandResult(accepted=False, message=f"invalid angular size {arcsec!r}")
        if (axis, direction) not in self._rates:
            return CommandResult(
                accepted=False, message="no centering rate installed for this direction"
            )
        positive = direction is AxisDirection.POSITIVE
        offset = arcsec if positive else -arcsec  # RA + = east, Dec + = north
        self._cancel.clear()
        try:
            if axis == MountAxis.AXIS1:
                result = mount.move_ra(offset, mode="center", cancel_check=self._cancel.is_set)
            else:
                result = mount.move_dec(offset, mode="center", cancel_check=self._cancel.is_set)
        except OnStepSafetyError as exc:
            return CommandResult(accepted=False, message=f"OnStepAdapter refused the move: {exc}")
        except ValueError as exc:
            return CommandResult(accepted=False, message=str(exc))
        return self._verdict(result)

    @staticmethod
    def _verdict(result: AxisMotionResult) -> CommandResult:
        if result.cancelled:
            return CommandResult(accepted=False, message="pulse cancelled")
        if not result.ok:
            return CommandResult(
                accepted=False, message=result.error or "OnStepAdapter reported failure"
            )
        return CommandResult(accepted=True)

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
        return self._verdict(result)
