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

`pulse_axis()` checks whether the driver actually accepted the motion
switch before sleeping out the pulse duration, rather than assuming it
always succeeds. Sourced, not guessed: libindi's own
`INDI::Telescope::MoveNS`/`MoveWE` (`inditelescope.cpp`) rejects the
command outright while parked -- `MovementNSSP.setState(IPS_IDLE);
MovementNSSP.apply(); return false;` -- resetting the switch element
back off and reporting the vector's state as `Idle`, not `Ok`, rather
than silently accepting it. Confirmed directly against the real rig too:
sending `MOTION_EAST=On` to a parked mount came back `MOTION_EAST=Off`/
`state=Idle`. Without this check, a caller had no way to tell a genuine
pulse from one the driver quietly refused.
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
#: Now a wait-for-confirmation ceiling, not a blind sleep -- pulse_axis()
#: waits for the driver to actually echo the newly-selected rate back
#: (returning as soon as it does, not always waiting the full window),
#: bounded by this. Raised from the original 0.2s -- real report
#: (rate-7/48x pulses only ever producing a couple of pixels of shift,
#: two orders of magnitude short of what 48x sidereal should cover)
#: raised the question of whether 0.2s was ever enough for the rate
#: change to really land before the motion command fired; this rig's
#: OnStep driver is already documented (IndiMountParkAdapter's own
#: incident 25446102) to apply some property changes with a real
#: multi-second delay.
_RATE_SELECT_SETTLE_S = 2.0
#: How long to wait for TELESCOPE_MOTION_NS/_WE to confirm a pulse's
#: motion-on switch -- either the element actually turning "On" (genuine
#: accept), or the vector reporting state="Idle" (libindi's own
#: MoveNS/MoveWE rejection, e.g. while parked -- see pulse_axis()'s own
#: comment). A real transition either way should be near-instant (it's
#: not a physical settle the way _RATE_SELECT_SETTLE_S's rate change can
#: be), so this is a generous ceiling against a wedged connection, not a
#: value tuned against observed real timing.
_MOTION_CONFIRM_TIMEOUT_S = 2.0

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

        # Real report: "have all axis being moved really? Move guide left
        # failed without movement" -- this method previously logged
        # nothing at all, so a diagnostic bundle could never actually
        # prove whether AXIS2's own pulse was sent, at what rate, for how
        # long, only that *something* in the calibration sequence reached
        # this far (via IndiMountParkAdapter's own unpark() log lines).
        # Mirrors IndiFocuserAdapter's own "Logging" section (added for
        # exactly this class of "the log shows nothing about the moves in
        # question" report).
        _log.info(
            "IndiMountPulseAdapter.pulse_axis(): %s (%s) on %r via %s/%s, %dms @ rate %r",
            axis.name,
            direction.name,
            self._device_name,
            vector_name,
            element,
            clamped_ms,
            selected_rate,
        )
        previous_rate = self._current_slew_rate_element()
        self._client.send_new_switch_vector(
            self._device_name, "TELESCOPE_SLEW_RATE", {selected_rate: True}
        )
        # Real report: "Move fails as AXIS1 and AXIS2 movements are too
        # small... rate preset 7 seems very small... works via EKOS." The
        # math backs this up: preset "7"/48x sidereal should cover an
        # entire 3840px frame at Main's ~0.38"/px plate scale in well
        # under a second, not the single-digit-to-low-double-digit pixel
        # shifts actually observed over 1-2s pulses -- roughly two orders
        # of magnitude off. A blind fixed _RATE_SELECT_SETTLE_S sleep
        # before the motion command fires is one concrete, fixable
        # candidate: if the real rate change takes longer than that to
        # actually land on this rig (this driver is already documented,
        # via IndiMountParkAdapter's own incident 25446102, to apply some
        # property changes with a real multi-second delay), every pulse
        # so far would have run at whatever rate was already selected
        # beforehand -- not the one this call just asked for. Waits for
        # the driver to actually confirm the new rate instead of assuming
        # a fixed delay is enough; still bounded, and still proceeds
        # (logged) rather than blocking a pulse forever if it never
        # confirms -- not proof by itself that this was the whole story,
        # but a real gap either way.
        confirmed_rate = self._client.wait_for_vector(
            self._device_name,
            "TELESCOPE_SLEW_RATE",
            timeout_s=_RATE_SELECT_SETTLE_S,
            predicate=lambda v: v.elements.get(selected_rate) == "On",
        )
        if confirmed_rate is None:
            _log.warning(
                "IndiMountPulseAdapter.pulse_axis(): rate preset %r on %r did not confirm "
                "within %ss -- proceeding anyway; this pulse may run at the previous rate (%r)",
                selected_rate,
                self._device_name,
                _RATE_SELECT_SETTLE_S,
                previous_rate,
            )
        # Captured before sending -- TELESCOPE_MOTION_NS/_WE's own initial
        # defSwitchVector (at connect time) already reports state="Idle"
        # (elements all Off), the *same* state libindi uses for a genuine
        # rejection. Without pinning down "the cached vector as it stood
        # before this send", wait_for_vector's own check-current-value-
        # first behavior could immediately match that stale leftover
        # def-time Idle and misread it as this call's own rejection.
        previous_motion_vector = self._client.get_vector(self._device_name, vector_name)
        self._client.send_new_switch_vector(self._device_name, vector_name, {element: True})
        # Real finding, verified against both the real rig and libindi's
        # own INDI::Telescope::MoveNS/MoveWE source (inditelescope.cpp):
        # the base class rejects a motion command outright while parked --
        # resets the switch element back to Off and reports the vector's
        # state as "Idle", not "Ok" (confirmed live: sending MOTION_EAST=On
        # to the real parked mount came back MOTION_EAST=Off/state=Idle).
        # This call used to never check the response at all -- it sent,
        # blindly slept out the *entire* requested duration regardless of
        # whether the driver actually accepted the motion, and always
        # returned accepted=True. Now waits for the vector to either
        # confirm the element actually turned on, or report Idle (rejected)
        # -- whichever comes first -- and only sleeps out the pulse
        # duration once genuinely accepted.
        confirmed_motion = self._client.wait_for_vector(
            self._device_name,
            vector_name,
            timeout_s=_MOTION_CONFIRM_TIMEOUT_S,
            predicate=lambda v: v is not previous_motion_vector
            and (v.elements.get(element) == "On" or v.state == "Idle"),
        )
        accepted = confirmed_motion is not None and confirmed_motion.elements.get(element) == "On"
        if not accepted:
            _log.warning(
                "IndiMountPulseAdapter.pulse_axis(): %s (%s) on %r rejected -- mount may still "
                "be parked, or the driver never confirmed within %ss",
                axis.name,
                direction.name,
                self._device_name,
                _MOTION_CONFIRM_TIMEOUT_S,
            )
            if previous_rate is not None and previous_rate != selected_rate:
                self._client.send_new_switch_vector(
                    self._device_name, "TELESCOPE_SLEW_RATE", {previous_rate: True}
                )
            return CommandResult(
                accepted=False, message="mount rejected the motion command -- still parked?"
            )
        time.sleep(clamped_ms / 1000.0)
        # Same class of gap as the motion-on check above, on the other
        # side of the pulse -- arguably worse: if this specific send is
        # ever lost (dropped packet, driver hiccup, anything), the mount
        # keeps physically moving with nothing in this app aware of it,
        # let alone able to stop it. Verifies the driver actually
        # confirms turning it back off; if it doesn't within
        # _MOTION_CONFIRM_TIMEOUT_S, falls back to abort() -- the one
        # mechanism (TELESCOPE_ABORT_MOTION) that doesn't depend on this
        # specific switch send having landed at all.
        previous_motion_vector_off = self._client.get_vector(self._device_name, vector_name)
        self._client.send_new_switch_vector(self._device_name, vector_name, {element: False})
        confirmed_off = self._client.wait_for_vector(
            self._device_name,
            vector_name,
            timeout_s=_MOTION_CONFIRM_TIMEOUT_S,
            predicate=lambda v: v is not previous_motion_vector_off
            and v.elements.get(element) == "Off",
        )
        if confirmed_off is None:
            _log.error(
                "IndiMountPulseAdapter.pulse_axis(): %s (%s) on %r did not confirm turning "
                "motion OFF within %ss after the pulse -- calling abort() as a safety fallback",
                axis.name,
                direction.name,
                self._device_name,
                _MOTION_CONFIRM_TIMEOUT_S,
            )
            self.abort()
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
