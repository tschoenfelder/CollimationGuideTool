"""IndiFilterWheelAdapter — FilterWheelPort backed by a real indiserver
connection. Issue #34 shipped read-only status display; issue #47 adds
commanding (`set_slot`).

Structurally a smaller copy of `astrotool_core.focus.indi_focuser_
adapter.IndiFocuserAdapter` (see that module's own docstring for the
INDI-wire-protocol background) targeting libindi's standard Filter
Wheel Interface properties: `CONNECTION` (standard), `FILTER_SLOT`
(number vector, element `FILTER_SLOT_VALUE` -- current/target slot,
`Busy` state while moving), `FILTER_NAME` (text vector, elements
`FILTER_SLOT_NAME_<n>`, one per slot, 1-indexed).

`set_slot()` is fire-and-forget, mirroring `OnStepFocuserAdapter.move()` --
not a blocking call, since INDI's own model is inherently async (send a
`newNumberVector`, watch it change over subsequent pushes) and blocking here
would freeze the GUI thread for the length of a real wheel rotation. The
caller polls `status()` for `moving`/`current_slot` to confirm arrival.

No real EFW hardware has been identified for this rig yet -- `device_name`
below is an unverified placeholder. Same "hypothesis pending Pi
verification" caveat this project already applies to new adapters (see
memory `feedback_verify_hardware_hypotheses_before_shipping`): treat
the property names above as sourced from libindi's general Filter
Wheel Interface convention, not confirmed against this rig's actual
driver.

Every public method tolerates being called before `connect()` succeeds,
or when no filter wheel hardware was found behind the connected INDI
device -- returns safe defaults, never raises, matching `NoFilterWheel`/
`FakeFilterWheel`'s tolerance and the shared `FilterWheelPort` contract
test's expectations.
"""

from __future__ import annotations

import contextlib
import logging
import time

from astrotool_core.filter_wheel.port import FilterWheelPort, FilterWheelState
from astrotool_core.indi.client import IndiClient, VectorState

_log = logging.getLogger(__name__)

_DEFAULT_DEVICE_NAME = "Filter Wheel"
_DEFAULT_PORT = 7624
_CONNECT_TIMEOUT_S = 10.0
_FILTER_WHEEL_PROBE_TIMEOUT_S = 3.0
#: Same gap/fix as IndiFocuserAdapter._PROPERTY_REFRESH_INTERVAL_S (see
#: that constant's own docstring) -- IndiClient only ever tracks what
#: the driver last *pushed*, never re-asks on its own. status() re-sends
#: getProperties for this device at most once every this many seconds,
#: so an externally-driven filter change (issue's own "if the EFW
#: changes position externally... the displayed state should update
#: accordingly once the application receives/refetches the new device
#: state") can't be cached forever with no way to notice.
_PROPERTY_REFRESH_INTERVAL_S = 2.0


class IndiFilterWheelAdapter(FilterWheelPort):
    def __init__(
        self,
        host: str = "localhost",
        port: int = _DEFAULT_PORT,
        device_name: str = _DEFAULT_DEVICE_NAME,
        *,
        connect_timeout_s: float = _CONNECT_TIMEOUT_S,
        filter_names: dict[int, str] | None = None,
        names_override: bool = False,
    ) -> None:
        self._device_name = device_name
        self._connect_timeout_s = connect_timeout_s
        self._client = IndiClient(host, port)
        self._connected = False
        self._available = False
        self._last_property_refresh: float = 0.0
        #: Issue #47: config-supplied slot -> name fallback, used only when
        #: the device itself reports no name for that slot -- see
        #: _lookup_filter_name/slot_names. Device-reported state always wins.
        self._configured_filter_names: dict[int, str] = dict(filter_names or {})
        #: True -> the configured names beat the device-reported ones (the
        #: driver's own defaults did not match this rig's physical wheel).
        self._names_override = names_override

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
                f"IndiFilterWheelAdapter: {self._device_name!r} did not confirm CONNECTION "
                f"within {self._connect_timeout_s}s — is indiserver running with this driver?"
            )
        self._connected = True
        # The driver only defines the filter-wheel vectors once it has
        # probed for real hardware post-connect (mirrors
        # IndiFocuserAdapter's own FOCUS_MAX probe) -- a short wait, and
        # *not* available is a graceful outcome, not a connection failure.
        slot_vector = self._client.wait_for_vector(
            self._device_name, "FILTER_SLOT", timeout_s=_FILTER_WHEEL_PROBE_TIMEOUT_S
        )
        self._available = slot_vector is not None
        if not self._available:
            _log.warning(
                "IndiFilterWheelAdapter: %r connected but no filter wheel hardware detected",
                self._device_name,
            )
        else:
            _log.info("IndiFilterWheelAdapter: connected to %r", self._device_name)

    def disconnect(self) -> None:
        if self._connected:
            # Best-effort -- a dropped connection must not crash teardown
            # (real incident b6d3384b), same convention as the focuser
            # and mount adapters.
            with contextlib.suppress(ConnectionError, OSError):
                self._client.send_new_switch_vector(
                    self._device_name, "CONNECTION", {"DISCONNECT": True}
                )
        self._client.close()
        self._connected = False
        self._available = False

    @property
    def is_available(self) -> bool:
        return self._connected and self._available

    def status(self) -> FilterWheelState:
        self._maybe_refresh_properties()
        if not self._connected:
            return FilterWheelState(
                available=False,
                current_slot=None,
                filter_name=None,
                moving=False,
                reason="not connected",
            )
        if not self._available:
            return FilterWheelState(
                available=False,
                current_slot=None,
                filter_name=None,
                moving=False,
                reason="no filter wheel detected",
            )
        vector = self._client.get_vector(self._device_name, "FILTER_SLOT")
        slot = self._parse_slot(vector)
        if slot is None:
            return FilterWheelState(
                available=True,
                current_slot=None,
                filter_name=None,
                moving=False,
                reason="position unreadable",
            )
        return FilterWheelState(
            available=True,
            current_slot=slot,
            filter_name=self._lookup_filter_name(slot),
            moving=vector is not None and vector.state == "Busy",
            reason=None,
        )

    @staticmethod
    def _parse_slot(vector: VectorState | None) -> int | None:
        if vector is None:
            return None
        try:
            return int(float(vector.elements.get("FILTER_SLOT_VALUE", "")))
        except ValueError:
            return None

    def _lookup_filter_name(self, slot: int) -> str | None:
        """Device-reported name wins when present and non-empty; otherwise
        the config-supplied fallback for this slot, if any (issue #47)."""
        if self._names_override and slot in self._configured_filter_names:
            return self._configured_filter_names[slot]
        names_vector = self._client.get_vector(self._device_name, "FILTER_NAME")
        reported = names_vector.elements.get(f"FILTER_SLOT_NAME_{slot}") if names_vector else None
        if reported:
            return reported
        return self._configured_filter_names.get(slot)

    def slot_names(self) -> dict[int, str]:
        """Best-known name for every slot the device has (one
        `FILTER_SLOT_NAME_<n>` element per physical slot, per the standard
        libindi Filter Wheel Interface -- unverified against this rig's real
        driver, see the module docstring's own hardware caveat), falling
        back to the configured name per slot. {} before the device has
        defined `FILTER_NAME` (not yet connected, or no wheel detected)."""
        if self._names_override:
            return dict(self._configured_filter_names)
        names_vector = self._client.get_vector(self._device_name, "FILTER_NAME")
        if names_vector is None:
            return dict(self._configured_filter_names)
        slots: dict[int, str] = {}
        for key, value in names_vector.elements.items():
            if not key.startswith("FILTER_SLOT_NAME_"):
                continue
            try:
                slot = int(key.removeprefix("FILTER_SLOT_NAME_"))
            except ValueError:
                continue
            slots[slot] = value or self._configured_filter_names.get(slot, "")
        return {slot: name for slot, name in slots.items() if name}

    def set_slot(self, slot: int) -> None:
        if not (self._connected and self._available):
            return
        if self.status().moving:
            raise RuntimeError("IndiFilterWheelAdapter: a filter move is already in progress")
        self._client.send_new_number_vector(
            self._device_name, "FILTER_SLOT", {"FILTER_SLOT_VALUE": float(slot)}
        )

    def _maybe_refresh_properties(self) -> None:
        if not self._connected:
            return
        now = time.monotonic()
        if now - self._last_property_refresh < _PROPERTY_REFRESH_INTERVAL_S:
            return
        self._last_property_refresh = now
        with contextlib.suppress(ConnectionError, OSError):
            self._client.send_get_properties(self._device_name)
