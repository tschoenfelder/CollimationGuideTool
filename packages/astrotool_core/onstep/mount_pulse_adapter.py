"""OnStepMountPulseAdapter — `MountPort` over OnStepAdapter's INDI-backed
finite axis motion (>= this dev build of 0.4.0).

Raw protocol sequencing (GOTO issuance, RA/Dec feedback verification,
size-scaled arrival tolerances) stays inside OnStepAdapter
(`IndiMount.move_ra_axis_deg`/`move_dec_axis_deg`); this shim only maps
axis/direction to a signed degree offset and reports the adapter's verdict.

One real capability gap versus 0.3.5, intentional -- not something this
shim works around locally (AGENTS.md: extend/fix OnStepAdapter, don't build
a parallel implementation here): `pulse_axis` (a timed move at an installed
rate) has **no** INDI equivalent -- the new primitive is a verified
degree-target GOTO, not a duration-at-a-rate pulse.
`capabilities().supports_pulse_guiding` is `False` and `pulse_axis` always
refuses.

`move_angular` works for offsets from 30" to 36000" (10 degrees) --
OnStepAdapter's own bound (raised from an original 720" floor after
OnStepAdapter#14: every seed in AGENTS.md's Mount Align policy now clears
it). Requests outside that range are refused with an explicit message,
never silently clamped.

`install_rate`/`installed_rate` are accepted-but-unused no-ops: the new
primitive takes a target offset directly and needs no arcsec/s rate to
convert a timed pulse into a distance, unlike 0.3.5's `move_ra`/`move_dec`.
"""

from __future__ import annotations

import math
import threading

from onstep_adapter import IndiMount

from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.onstep.connection import OnStepConnection

#: OnStepAdapter's own `IndiAxisMover` bound (30"..10 degrees).
_MIN_AXIS_ARCSEC = 30.0
_MAX_AXIS_ARCSEC = 36000.0

_NO_PULSE_PRIMITIVE = (
    "OnStepAdapter has no timed pulse primitive over INDI yet "
    "(tracked as an OnStepAdapter enhancement request)"
)


class OnStepMountPulseAdapter:
    def __init__(self, connection: OnStepConnection) -> None:
        self._connection = connection
        self._held = False
        self._cancel = threading.Event()
        #: Accepted but never consulted -- see module docstring.
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
        return MountCapabilities(supports_pulse_guiding=False, min_pulse_ms=0, max_pulse_ms=0)

    def _mount(self) -> IndiMount | None:
        client = self._connection.client
        return client.mount if self._held and client is not None else None

    def status(self) -> MountStatus:
        mount = self._mount()
        if mount is None:
            return MountStatus(connected=False, tracking=False, slewing=False)
        snapshot = mount.get_status()
        return MountStatus(connected=True, tracking=snapshot.tracking, slewing=snapshot.slewing)

    def abort(self) -> None:
        """Cancel a running move: cooperative cancel first, then OnStep's own stop."""
        self._cancel.set()
        mount = self._mount()
        if mount is not None:
            mount.stop()

    # ---- AngularMotionPort ------------------------------------------------
    def installed_rate(self, axis: MountAxis, direction: AxisDirection) -> float | None:
        return self._rates.get((axis, direction))

    def install_rate(self, axis: MountAxis, direction: AxisDirection, arcsec_per_s: float) -> None:
        """Recorded for API compatibility only -- OnStepAdapter's degree-target
        move needs no rate. Still validates input, matching 0.3.5's contract."""
        if not (0.0 < arcsec_per_s < float("inf")):
            raise ValueError(f"invalid centering rate {arcsec_per_s!r}")
        self._rates[(axis, direction)] = float(arcsec_per_s)

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        mount = self._mount()
        if mount is None:
            return CommandResult(accepted=False, message="not connected")
        if not (arcsec > 0.0) or not math.isfinite(arcsec):
            return CommandResult(accepted=False, message=f"invalid angular size {arcsec!r}")
        if arcsec < _MIN_AXIS_ARCSEC or arcsec > _MAX_AXIS_ARCSEC:
            return CommandResult(
                accepted=False,
                message=(
                    f"{arcsec:.1f}\" is outside OnStepAdapter's supported axis-move range "
                    f"({_MIN_AXIS_ARCSEC:.0f}\"-{_MAX_AXIS_ARCSEC:.0f}\")"
                ),
            )
        positive = direction is AxisDirection.POSITIVE
        offset_deg = (arcsec if positive else -arcsec) / 3600.0
        self._cancel.clear()
        try:
            if axis == MountAxis.AXIS1:
                mount.move_ra_axis_deg(offset_deg)
            else:
                mount.move_dec_axis_deg(offset_deg)
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:
            return CommandResult(accepted=False, message=str(exc))
        return CommandResult(accepted=True)

    # ---- MountPort ----------------------------------------------------
    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        return CommandResult(accepted=False, message=_NO_PULSE_PRIMITIVE)
