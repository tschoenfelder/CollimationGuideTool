"""IndiMountPulseAdapter — MountPort implementation backed by a real
indiserver connection, for the "test move" axis-calibration feature
(pulse the mount briefly, measure how far a star moved in-frame).

A separate `IndiClient` connection from `IndiMountParkAdapter`'s, same as
that adapter is separate from `IndiFocuserAdapter`'s — each adapter owns
its own connection to the same `"LX200 OnStep"` device, and INDI has no
trouble with multiple simultaneous clients on one device.

Unlike the existing `IndiMountAdapter` (native OnStep serial via
onstep-adapter, `# pragma: no cover — requires a real OnStep mount`
throughout, never wired into either app), this speaks real INDI, matching
this project's established preference for the focuser and mount-park
adapters, and is fully unit-testable against `FakeIndiServer`.

Drives libindi's standard Telescope Interface motion properties —
confirmed present on the real rig via a live read-only probe:
`TELESCOPE_SLEW_RATE` (a 10-preset `OneOfMany` switch vector, elements
named "0".."9") and `TELESCOPE_MOTION_NS`/`TELESCOPE_MOTION_WE` (plain
on/off direction switches, no built-in duration — unlike the standard
`TELESCOPE_TIMED_GUIDE_NS`/`_WE` number vectors, which self-time but move
at the separate, much slower `GUIDE_RATE`). The probe found preset
element `"6"` labelled `"20x"` (probe output: `0=0.25x, 1=0.5x, 2=1x,
3=2x, 4=4x, 5=8x, 6=20x, 7=48x, 8=Half-Max, 9=Max`) — this adapter
defaults to that preset since the axis-calibration feature this exists
for was specced against "rate 20" meaning 20x sidereal.

`pulse_axis()` therefore has to do its own timing (select the rate, turn
the direction switch on, sleep for `duration_ms`, turn it back off) and
so blocks the caller for the full pulse duration — deliberately, so a
caller (`axis_calibration.calibrate_axis()`, in particular) that measures
position immediately after `pulse_axis()` returns is measuring truly
after the move, not mid-slew. Restores whatever slew-rate preset was
selected before the pulse once done, since this is meant to be a
non-disruptive diagnostic probe, not a standing rate change.

`pulse_axis()`'s optional `rate_preset` overrides `self._slew_rate_element`
for that one call only (still restored afterward like any other rate) —
added for the mount-alignment feature (`MountTestMovePanel`), which needs
every calibration/nudge pulse to run at one deliberately-configured rate
(default "7"/48x, see `astrotool_core.config.mount_alignment_settings`)
independent of whatever this instance's own default happens to be.

Direction mapping (documented since it's this adapter's own convention,
`MountPort` itself is direction-agnostic): AXIS1 (RA/azimuth) POSITIVE =
east, NEGATIVE = west; AXIS2 (Dec/altitude) POSITIVE = north, NEGATIVE =
south — matches `indi_adapter.py`'s existing e/w/n/s convention for the
same (axis, direction) pairs.
"""

from __future__ import annotations

import logging
import time

from astrotool_core.indi.client import IndiClient
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)

_log = logging.getLogger(__name__)

_DEFAULT_DEVICE_NAME = "LX200 OnStep"
_DEFAULT_PORT = 7624
_CONNECT_TIMEOUT_S = 10.0
_MOUNT_PROBE_TIMEOUT_S = 3.0
_MIN_PULSE_MS = 1
_MAX_PULSE_MS = 9999
#: The "20x" preset element of TELESCOPE_SLEW_RATE -- see module docstring.
_DEFAULT_SLEW_RATE_ELEMENT = "6"
_RATE_SELECT_SETTLE_S = 0.2

_MOTION_VECTOR: dict[tuple[MountAxis, AxisDirection], tuple[str, str]] = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): ("TELESCOPE_MOTION_WE", "MOTION_EAST"),
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): ("TELESCOPE_MOTION_WE", "MOTION_WEST"),
    (MountAxis.AXIS2, AxisDirection.POSITIVE): ("TELESCOPE_MOTION_NS", "MOTION_NORTH"),
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): ("TELESCOPE_MOTION_NS", "MOTION_SOUTH"),
}


class IndiMountPulseAdapter:
    """MountPort backed by real INDI directional-motion properties."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = _DEFAULT_PORT,
        device_name: str = _DEFAULT_DEVICE_NAME,
        *,
        slew_rate_element: str = _DEFAULT_SLEW_RATE_ELEMENT,
        connect_timeout_s: float = _CONNECT_TIMEOUT_S,
    ) -> None:
        self._device_name = device_name
        self._slew_rate_element = slew_rate_element
        self._connect_timeout_s = connect_timeout_s
        self._client = IndiClient(host, port)
        self._connected = False
        self._available = False

    def connect(self) -> None:
        self._client.connect()
        self._client.send_get_properties(self._device_name)
        self._client.send_new_switch_vector(self._device_name, "CONNECTION", {"CONNECT": True})
        connection = self._client.wait_for_vector(
            self._device_name,
            "CONNECTION",
            timeout_s=self._connect_timeout_s,
            predicate=lambda v: v.elements.get("CONNECT") == "On",
        )
        if connection is None:
            self._client.close()
            raise ConnectionError(
                f"IndiMountPulseAdapter: {self._device_name!r} did not confirm CONNECTION "
                f"within {self._connect_timeout_s}s — is indiserver running with this driver?"
            )
        self._connected = True
        motion_vector = self._client.wait_for_vector(
            self._device_name, "TELESCOPE_MOTION_NS", timeout_s=_MOUNT_PROBE_TIMEOUT_S
        )
        self._available = motion_vector is not None
        if not self._available:
            _log.warning(
                "IndiMountPulseAdapter: %r connected but no mount interface detected",
                self._device_name,
            )
        else:
            _log.info("IndiMountPulseAdapter: connected to %r", self._device_name)

    def disconnect(self) -> None:
        if self._connected:
            self._client.send_new_switch_vector(
                self._device_name, "CONNECTION", {"DISCONNECT": True}
            )
        self._client.close()
        self._connected = False
        self._available = False

    @property
    def is_available(self) -> bool:
        return self._connected and self._available

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            supports_pulse_guiding=True, min_pulse_ms=_MIN_PULSE_MS, max_pulse_ms=_MAX_PULSE_MS
        )

    def status(self) -> MountStatus:
        if not self.is_available:
            return MountStatus(connected=False, tracking=False, slewing=False)
        return MountStatus(connected=True, tracking=False, slewing=False)

    def abort(self) -> None:
        """Immediately stop any in-progress motion — not part of the
        `MountPort` Protocol (that's the architecture doc's literal
        contract, not something to extend unilaterally), so callers that
        want this reach for it directly on this concrete class, or
        duck-type it (`MountTestMovePanel`'s "Stop" button does the
        latter, since it's typed against `MountPort` generically).
        `pulse_axis()`'s own blocking sleep can't be interrupted, but the
        *physical* motion stops right away regardless — the sleep just
        finishes out harmlessly (turning an already-stopped direction
        switch off again, restoring the rate) once its time is up."""
        if not self.is_available:
            return
        _log.info("IndiMountPulseAdapter.abort(): aborting motion on %r", self._device_name)
        self._client.send_new_switch_vector(
            self._device_name, "TELESCOPE_ABORT_MOTION", {"ABORT": True}
        )

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        if not self.is_available:
            return CommandResult(accepted=False, message="not connected")
        vector_name, element = _MOTION_VECTOR[(axis, direction)]
        clamped_ms = max(_MIN_PULSE_MS, min(_MAX_PULSE_MS, duration_ms))
        selected_rate = rate_preset if rate_preset is not None else self._slew_rate_element

        previous_rate = self._current_slew_rate_element()
        self._client.send_new_switch_vector(
            self._device_name, "TELESCOPE_SLEW_RATE", {selected_rate: True}
        )
        time.sleep(_RATE_SELECT_SETTLE_S)
        self._client.send_new_switch_vector(self._device_name, vector_name, {element: True})
        time.sleep(clamped_ms / 1000.0)
        self._client.send_new_switch_vector(self._device_name, vector_name, {element: False})
        if previous_rate is not None and previous_rate != selected_rate:
            self._client.send_new_switch_vector(
                self._device_name, "TELESCOPE_SLEW_RATE", {previous_rate: True}
            )
        return CommandResult(accepted=True)

    def _current_slew_rate_element(self) -> str | None:
        vector = self._client.get_vector(self._device_name, "TELESCOPE_SLEW_RATE")
        if vector is None:
            return None
        for name, value in vector.elements.items():
            if value == "On":
                return name
        return None
