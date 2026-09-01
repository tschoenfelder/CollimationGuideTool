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

Logging (issue: focuser jitter reports where the app's own log showed
nothing at all about the moves in question): `connect()` turns the
driver's own `DEBUG`/`DEBUG_LEVEL` properties fully on — indiserver's own
`<date>.islog` then captures the driver's internal view of every move in
detail, which is what actually let a later real-hardware repro get
traced at the wire level. Every `move()`/`move_absolute()`/`stop()` call
is also logged here at INFO, so this app's *own* diagnostic bundle
(`application.log`) has a record too, not just indiserver's separate log
file.

`move_absolute()` checks whether the driver actually accepted the move
before reporting success, rather than assuming it always does. Sourced,
not guessed: libindi's own `INDI::FocuserInterface`
(`indifocuserinterface.cpp`) applies `MoveAbsFocuser()`/
`MoveRelFocuser()`'s returned `IPState` directly to
`ABS_FOCUS_POSITION`'s own vector state -- `IPS_ALERT` means rejected,
`IPS_BUSY`/`IPS_OK` mean accepted (started, or already done). Same class
of gap `IndiMountPulseAdapter.pulse_axis()` had (real report 1bb412ae's
investigation) -- without this check, a caller had no way to tell a
genuine move from one the driver rejected. `move()` (used for jog
In/Out) has the same underlying gap but no return value at all
(`FocuserPort.move() -> None`) to report it through -- a bigger,
not-yet-decided change to the shared `FocuserPort` Protocol itself,
left alone here.
"""

from __future__ import annotations

import contextlib
import logging
import time

from astrotool_core.focus.port import FocuserMoveResult, FocuserPort, FocuserStatus
from astrotool_core.indi.client import IndiClient, VectorState

_log = logging.getLogger(__name__)

_DEFAULT_DEVICE_NAME = "LX200 OnStep"
_DEFAULT_PORT = 7624
_CONNECT_TIMEOUT_S = 10.0
_FOCUSER_PROBE_TIMEOUT_S = 3.0
#: Real report: "Calling the APP still shows focuser as moving ... The
#: state should always been taken from indiserver." Same gap as
#: `IndiMountParkAdapter._PROPERTY_REFRESH_INTERVAL_S` (see that constant's
#: docstring for the full reasoning) -- `IndiClient` only ever tracks what
#: the driver last *pushed*, and never re-asks on its own. `status()` now
#: re-sends getProperties for this device at most once every this many
#: seconds, so a stale/stuck ABS_FOCUS_POSITION Busy state can't be cached
#: forever with no way to notice.
_PROPERTY_REFRESH_INTERVAL_S = 2.0
#: How long move_absolute() waits for ABS_FOCUS_POSITION to confirm
#: accepting or rejecting a move -- see that method's own comment.
#: Should be near-instant either way (the driver applies the returned
#: IPState synchronously in response to the request), so this is a
#: generous ceiling against a wedged connection, not a value tuned
#: against observed real timing.
_MOVE_CONFIRM_TIMEOUT_S = 2.0


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
                f"IndiFocuserAdapter: {self._device_name!r} did not confirm CONNECTION "
                f"within {self._connect_timeout_s}s — is indiserver running with this driver?"
            )
        self._connected = True
        self._enable_verbose_driver_logging()
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
        else:
            _log.info(
                "IndiFocuserAdapter: connected to %r at position=%d, max=%d",
                self._device_name,
                self.get_position(),
                self.get_max_position(),
            )

    def _enable_verbose_driver_logging(self) -> None:
        """Turn the driver's own DEBUG output fully on, so indiserver's
        log file captures maximum detail about what the driver/firmware
        actually did for every move — see module docstring's "Logging".
        Best-effort: these properties existing at all is a driver detail
        this adapter otherwise doesn't depend on, so a failure here must
        never block a successful connect.
        """
        try:
            self._client.send_new_switch_vector(self._device_name, "DEBUG", {"ENABLE": True})
            self._client.send_new_switch_vector(
                self._device_name,
                "DEBUG_LEVEL",
                {
                    "DBG_ERROR": True,
                    "DBG_WARNING": True,
                    "DBG_SESSION": True,
                    "DBG_DEBUG": True,
                    "DBG_EXTRA_1": True,
                    "DBG_EXTRA_2": True,
                },
            )
        except ConnectionError:
            _log.warning(
                "IndiFocuserAdapter: could not enable verbose driver logging", exc_info=True
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
        self._maybe_refresh_properties()
        return FocuserStatus(
            available=self.is_available,
            position=self.get_position(),
            max_position=self.get_max_position(),
            moving=self.is_moving(),
        )

    def _maybe_refresh_properties(self) -> None:
        # See _PROPERTY_REFRESH_INTERVAL_S's docstring -- forces indiserver
        # to re-announce this device's properties so a stale/stuck value
        # can't be cached forever. Throttled so a UI polling status()
        # frequently doesn't hammer the driver.
        if not self._connected:
            return
        now = time.monotonic()
        if now - self._last_property_refresh < _PROPERTY_REFRESH_INTERVAL_S:
            return
        self._last_property_refresh = now
        with contextlib.suppress(ConnectionError, OSError):
            self._client.send_get_properties(self._device_name)

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
        _log.info(
            "IndiFocuserAdapter.move(): steps=%d (from position=%d)", steps, self.get_position()
        )
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
        _log.info(
            "IndiFocuserAdapter.move_absolute(): target=%d (from position=%d)",
            steps,
            start_position,
        )
        # Real finding, sourced against libindi's own INDI::FocuserInterface
        # (indifocuserinterface.cpp), not guessed: MoveAbsFocuser()/
        # MoveRelFocuser() returning IPS_ALERT gets applied directly to
        # ABS_FOCUS_POSITION's own vector state -- unlike
        # IndiMountPulseAdapter's motion switches (which reset the element
        # back off on rejection), the requested value here is left in
        # place; only the state distinguishes a genuine accept (Busy/Ok)
        # from a rejection (Alert). This call used to never check the
        # response at all -- it sent, then always returned accepted=True
        # regardless of what the driver actually did.
        previous_vector = self._client.get_vector(self._device_name, "ABS_FOCUS_POSITION")
        self._client.send_new_number_vector(
            self._device_name, "ABS_FOCUS_POSITION", {"FOCUS_ABSOLUTE_POSITION": steps}
        )
        confirmed = self._client.wait_for_vector(
            self._device_name,
            "ABS_FOCUS_POSITION",
            timeout_s=_MOVE_CONFIRM_TIMEOUT_S,
            predicate=lambda v: v is not previous_vector and v.state in ("Busy", "Ok", "Alert"),
        )
        accepted = confirmed is not None and confirmed.state != "Alert"
        if not accepted:
            _log.warning(
                "IndiFocuserAdapter.move_absolute(): target=%d on %r rejected (state=%s)",
                steps,
                self._device_name,
                confirmed.state if confirmed is not None else "unconfirmed",
            )
        return FocuserMoveResult(
            accepted=accepted, target_position=steps, start_position=start_position
        )

    def stop(self) -> None:
        if not self.is_available:
            return
        _log.info(
            "IndiFocuserAdapter.stop(): aborting motion at position=%d", self.get_position()
        )
        self._client.send_new_switch_vector(
            self._device_name, "FOCUS_ABORT_MOTION", {"ABORT": True}
        )

    def _get_abs_position_vector(self) -> VectorState | None:
        if not self.is_available:
            return None
        return self._client.get_vector(self._device_name, "ABS_FOCUS_POSITION")
