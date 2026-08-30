"""IndiFocuserAdapter — FocuserPort backed by a real indiserver connection.

Unlike `astrotool_core/mount/indi_adapter.py` (whose own docstring
explains it does *not* speak the INDI wire protocol despite its name),
this adapter genuinely does: it drives an `astrotool_core.indi.IndiClient`
against libindi's standard Focuser Interface properties (`CONNECTION`,
`ABS_FOCUS_POSITION`, `FOCUS_MAX`, `FOCUS_MOTION`, `REL_FOCUS_POSITION`,
`FOCUS_ABORT_MOTION` — confirmed present in the real rig's
`indi_lx200_OnStep` driver via its linked `libindidriver.so`) against an
indiserver process this app never starts or manages itself — it is
expected to already be running (e.g. `indiserver -v indi_lx200_OnStep`),
on the Raspberry Pi this app runs on, hence `host="localhost"` by default.

Sign convention for `move()`/`move_absolute()`: **positive `steps` means
outward, negative means inward** — an otherwise-arbitrary choice (the
port itself has no fixed convention; `FakeFocuser` just adds/sets a raw
number), documented here since it drives real hardware.

Every public method tolerates being called before `connect()` succeeds,
or when the focuser was never found available (no focuser hardware
behind the connected INDI device) — returns safe defaults / rejects the
action, never raises, matching `NoFocuser`/`FakeFocuser`'s tolerance and
the shared `FocuserPort` contract test's expectations.
"""

from __future__ import annotations

import logging

from astrotool_core.focus.port import FocuserMoveResult, FocuserPort, FocuserStatus
from astrotool_core.indi.client import IndiClient, VectorState

_log = logging.getLogger(__name__)

_DEFAULT_DEVICE_NAME = "LX200 OnStep"
_DEFAULT_PORT = 7624
_CONNECT_TIMEOUT_S = 10.0
_FOCUSER_PROBE_TIMEOUT_S = 3.0


class IndiFocuserAdapter(FocuserPort):
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
                f"IndiFocuserAdapter: {self._device_name!r} did not confirm CONNECTION "
                f"within {self._connect_timeout_s}s — is indiserver running with this driver?"
            )
        self._connected = True
        # The driver only defines the focuser vectors once it has probed
        # for real focuser hardware post-connect (mirrors onstep-adapter's
        # own `:FA#` availability check) — a short wait, and *not*
        # available is a graceful outcome, not a connection failure.
        max_vector = self._client.wait_for_vector(
            self._device_name, "FOCUS_MAX", timeout_s=_FOCUSER_PROBE_TIMEOUT_S
        )
        self._available = max_vector is not None
        if not self._available:
            _log.warning(
                "IndiFocuserAdapter: %r connected but no focuser hardware detected",
                self._device_name,
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

    def status(self) -> FocuserStatus:
        return FocuserStatus(
            available=self.is_available,
            position=self.get_position(),
            max_position=self.get_max_position(),
            moving=self.is_moving(),
        )

    def get_position(self) -> int:
        vector = self._get_abs_position_vector()
        if vector is None:
            return 0
        try:
            return int(float(vector.elements.get("FOCUS_ABSOLUTE_POSITION", "0")))
        except ValueError:
            return 0

    def get_max_position(self) -> int:
        if not self.is_available:
            return 0
        vector = self._client.get_vector(self._device_name, "FOCUS_MAX")
        if vector is None:
            return 0
        try:
            return int(float(vector.elements.get("FOCUS_MAX_VALUE", "0")))
        except ValueError:
            return 0

    def is_moving(self) -> bool:
        vector = self._get_abs_position_vector()
        return vector is not None and vector.state == "Busy"

    def move(self, steps: int) -> None:
        if not self.is_available or steps == 0:
            return
        self._client.send_new_switch_vector(
            self._device_name,
            "FOCUS_MOTION",
            {"FOCUS_INWARD": steps < 0, "FOCUS_OUTWARD": steps >= 0},
        )
        self._client.send_new_number_vector(
            self._device_name, "REL_FOCUS_POSITION", {"FOCUS_RELATIVE_POSITION": abs(steps)}
        )

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        start_position = self.get_position()
        if not self.is_available:
            return FocuserMoveResult(
                accepted=False, target_position=steps, start_position=start_position
            )
        self._client.send_new_number_vector(
            self._device_name, "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": steps}
        )
        return FocuserMoveResult(
            accepted=True, target_position=steps, start_position=start_position
        )

    def stop(self) -> None:
        if not self.is_available:
            return
        self._client.send_new_switch_vector(
            self._device_name, "FOCUS_ABORT_MOTION", {"ABORT": True}
        )

    def _get_abs_position_vector(self) -> VectorState | None:
        if not self.is_available:
            return None
        return self._client.get_vector(self._device_name, "ABS_FOCUS_POSITION")
