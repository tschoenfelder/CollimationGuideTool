"""IndiMountParkAdapter — MountParkPort backed by a real indiserver
connection, connecting to the same device as
`astrotool_core.focus.indi_focuser_adapter.IndiFocuserAdapter` (the
OnStep mount and focuser share one physical controller and therefore one
INDI device, `"LX200 OnStep"` on this rig) — but a separate `IndiClient`
socket, since each adapter owns its own connection.

Drives libindi's standard Telescope Interface properties (`CONNECTION`,
`TELESCOPE_PARK`, `TELESCOPE_TRACK_STATE` — confirmed present on the real
rig's `indi_lx200_OnStep` driver via a live probe) against an indiserver
process this app never starts or manages itself, same as the focuser
adapter.

Deliberately minimal — park and unpark only, nothing else (no goto/sync/
slew/tracking-mode-selection): see `MountParkPort`'s own docstring for
why this is a separate port from the guiding-only `MountPort`, scoped
exactly to what was asked for.

`unpark()` immediately follows UNPARK with a TRACK_OFF command, rather
than trusting the mount's own post-unpark default (OnStep, like many
mounts, can auto-enable tracking as soon as it registers unparked) — sent
right after, not waiting for UNPARK to be confirmed first, so there is no
window where tracking could already be running unnoticed.

That first TRACK_OFF is not enough on its own, though: a real-hardware
trace (incident 25446102, "Shows unparked, tracking, but don't stop
tracking") caught OnStep's own driver overriding it — about 1.5s after
the adapter's TRACK_OFF, the driver pushes its *own* `TRACK_ON`/Busy
update as a side effect of unparking, and that state then persists
indefinitely (observed unchanged for a full 60s) since nothing ever
corrects it again. A single fire-and-forget TRACK_OFF sent alongside
UNPARK therefore loses this race. `unpark()` compensates by re-sending
TRACK_OFF a few more times over the following seconds
(`_TRACK_OFF_RETRY_DELAYS_S`), on a background timer, so whichever one
lands after the driver's own override still wins. Harmless if it turns
out the mount never overrides tracking on a given run (or was already
re-parked by the user by the time a retry fires) — TRACK_OFF is
idempotent.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time

from astrotool_core.indi.client import IndiClient
from astrotool_core.mount.park_port import MountParkPort, MountParkStatus

_log = logging.getLogger(__name__)

_DEFAULT_DEVICE_NAME = "LX200 OnStep"
_DEFAULT_PORT = 7624
_CONNECT_TIMEOUT_S = 10.0
_MOUNT_PROBE_TIMEOUT_S = 3.0
#: How long after UNPARK to re-send TRACK_OFF, to outlast OnStep's own
#: delayed auto-tracking-on override — see module docstring. Observed
#: override landed ~1.5s post-UNPARK on the real rig; three retries
#: spread out past that give margin for run-to-run timing variance
#: without hammering the driver.
_TRACK_OFF_RETRY_DELAYS_S = (2.0, 5.0, 10.0)
#: Real report: "Calling the APP still shows ... mount as being unparked,
#: even so I left it parked. The state should always been taken from
#: indiserver." IndiClient only ever tracks whatever a driver *pushes*
#: (def*Vector at connect, set*Vector on change) -- it never re-asks. If
#: the driver's very first TELESCOPE_PARK report (right after CONNECT,
#: while it may still be settling/querying the mount over serial) is
#: stale or wrong, and the driver has no reason of its own to push a
#: correction (nothing changed from *its* perspective), this app would
#: cache that wrong value forever with no way to notice. status() now
#: re-sends getProperties for this device at most once every this many
#: seconds, forcing the driver to re-announce its actual current
#: property values -- a normal, expected INDI client operation (real
#: INDI clients like KStars/Ekos do the same), not a driver-specific
#: workaround.
_PROPERTY_REFRESH_INTERVAL_S = 2.0


class IndiMountParkAdapter(MountParkPort):
    def __init__(
        self,
        host: str = "localhost",
        port: int = _DEFAULT_PORT,
        device_name: str = _DEFAULT_DEVICE_NAME,
        *,
        connect_timeout_s: float = _CONNECT_TIMEOUT_S,
    ) -> None:
        self._device_name = device_name
        self._connect_timeout_s = connect_timeout_s
        self._client = IndiClient(host, port)
        self._connected = False
        self._available = False
        self._pending_track_off_retries: list[threading.Timer] = []
        self._last_property_refresh: float = 0.0

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
                f"IndiMountParkAdapter: {self._device_name!r} did not confirm CONNECTION "
                f"within {self._connect_timeout_s}s — is indiserver running with this driver?"
            )
        self._connected = True
        park_vector = self._client.wait_for_vector(
            self._device_name, "TELESCOPE_PARK", timeout_s=_MOUNT_PROBE_TIMEOUT_S
        )
        self._available = park_vector is not None
        if not self._available:
            _log.warning(
                "IndiMountParkAdapter: %r connected but no mount interface detected",
                self._device_name,
            )
        else:
            status = self.status()
            _log.info(
                "IndiMountParkAdapter: connected to %r, parked=%s, tracking=%s",
                self._device_name,
                status.parked,
                status.tracking,
            )

    def disconnect(self) -> None:
        self._cancel_pending_track_off_retries()
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

    def status(self) -> MountParkStatus:
        self._maybe_refresh_properties()
        return MountParkStatus(
            available=self.is_available, parked=self._is_parked(), tracking=self._is_tracking()
        )

    def _maybe_refresh_properties(self) -> None:
        # See _PROPERTY_REFRESH_INTERVAL_S's docstring -- forces indiserver
        # to re-announce this device's properties so a stale/wrong initial
        # push (or a missed update) can't be cached forever. Throttled so
        # a UI polling status() frequently doesn't hammer the driver.
        if not self._connected:
            return
        now = time.monotonic()
        if now - self._last_property_refresh < _PROPERTY_REFRESH_INTERVAL_S:
            return
        self._last_property_refresh = now
        with contextlib.suppress(ConnectionError, OSError):
            self._client.send_get_properties(self._device_name)

    def _is_parked(self) -> bool:
        if not self.is_available:
            return False
        vector = self._client.get_vector(self._device_name, "TELESCOPE_PARK")
        return vector is not None and vector.elements.get("PARK") == "On"

    def _is_tracking(self) -> bool:
        if not self.is_available:
            return False
        vector = self._client.get_vector(self._device_name, "TELESCOPE_TRACK_STATE")
        return vector is not None and vector.elements.get("TRACK_ON") == "On"

    def park(self) -> None:
        if not self.is_available:
            return
        # Deliberately does NOT cancel unpark()'s pending TRACK_OFF
        # retries -- a real-hardware check (the "test move" feature,
        # which unparks/pulses/re-parks in well under the driver's own
        # ~1.5s delayed auto-track-on) caught park() cancelling them
        # right as one was about to be needed: the driver's override can
        # still land *after* this park() call, leaving tracking=True
        # despite parked=True. Any already-armed retries keep firing
        # (harmless/idempotent either way — see unpark()'s docstring);
        # unpark() itself still cancels stale ones from a previous cycle
        # before arming its own.
        _log.info("IndiMountParkAdapter.park(): parking %r", self._device_name)
        self._client.send_new_switch_vector(self._device_name, "TELESCOPE_PARK", {"PARK": True})

    def unpark(self) -> None:
        if not self.is_available:
            return
        _log.info(
            "IndiMountParkAdapter.unpark(): unparking %r and deactivating tracking",
            self._device_name,
        )
        self._cancel_pending_track_off_retries()
        self._client.send_new_switch_vector(self._device_name, "TELESCOPE_PARK", {"UNPARK": True})
        self._send_track_off()
        for delay_s in _TRACK_OFF_RETRY_DELAYS_S:
            timer = threading.Timer(delay_s, self._send_track_off)
            timer.daemon = True
            self._pending_track_off_retries.append(timer)
            timer.start()

    def stop_tracking(self) -> None:
        # Real report: mount left tracking after quitting the app --
        # closeEvent calls this (via MountParkPanel.stop()) before
        # disconnecting, rather than only the focuser/camera. Deliberately
        # does not park -- see MountParkPort.stop_tracking's own docstring
        # for why that stays a separate, explicit action.
        if not self.is_available:
            return
        _log.info(
            "IndiMountParkAdapter.stop_tracking(): deactivating tracking on %r (not parking)",
            self._device_name,
        )
        self._send_track_off()

    def _send_track_off(self) -> None:
        # Runs on the timer thread for retries -- the client/socket may
        # already be closed (disconnect, or a slow-to-cancel timer racing
        # it) by the time this fires, which would otherwise surface as an
        # unhandled exception on a background thread.
        with contextlib.suppress(ConnectionError, OSError):
            self._client.send_new_switch_vector(
                self._device_name, "TELESCOPE_TRACK_STATE", {"TRACK_OFF": True}
            )

    def _cancel_pending_track_off_retries(self) -> None:
        for timer in self._pending_track_off_retries:
            timer.cancel()
        self._pending_track_off_retries = []
