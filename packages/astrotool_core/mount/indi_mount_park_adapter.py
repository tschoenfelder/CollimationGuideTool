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
"""

from __future__ import annotations

import logging

from astrotool_core.indi.client import IndiClient
from astrotool_core.mount.park_port import MountParkPort, MountParkStatus

_log = logging.getLogger(__name__)

_DEFAULT_DEVICE_NAME = "LX200 OnStep"
_DEFAULT_PORT = 7624
_CONNECT_TIMEOUT_S = 10.0
_MOUNT_PROBE_TIMEOUT_S = 3.0


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
        return MountParkStatus(
            available=self.is_available, parked=self._is_parked(), tracking=self._is_tracking()
        )

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
        _log.info("IndiMountParkAdapter.park(): parking %r", self._device_name)
        self._client.send_new_switch_vector(self._device_name, "TELESCOPE_PARK", {"PARK": True})

    def unpark(self) -> None:
        if not self.is_available:
            return
        _log.info(
            "IndiMountParkAdapter.unpark(): unparking %r and deactivating tracking",
            self._device_name,
        )
        self._client.send_new_switch_vector(self._device_name, "TELESCOPE_PARK", {"UNPARK": True})
        self._client.send_new_switch_vector(
            self._device_name, "TELESCOPE_TRACK_STATE", {"TRACK_OFF": True}
        )
