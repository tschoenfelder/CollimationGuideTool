"""FakeIndiServer -- a real local TCP server speaking just enough of the
INDI wire protocol to drive `IndiClient`/`IndiFilterWheelAdapter` end-to-end
in tests, on any platform (no indiserver/libindi install needed).

Simulates ONE non-OnStep INDI device (default `"ToupTek EFW 1"`, the rig's
filter wheel) with a `CONNECTION` switch vector always present and -- only
after a simulated connect, mirroring real drivers' "interfaces probed after
connect" behaviour -- the libindi Filter Wheel Interface vectors
(`FILTER_NAME`, `FILTER_SLOT`), unless `filter_wheel_available=False`.

INDI is valid in this project only for such non-OnStep devices (cameras,
filter wheels): the OnStep controller (mount + focuser) is reached solely
through OnStepAdapter (AGENTS.md), so this double deliberately has no
mount/focuser/park/motion vectors.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading

from astrotool_core.indi._protocol import IncrementalIndiParser, ParsedElement, xml_escape_attr

_log = logging.getLogger(__name__)


class FakeIndiServer:
    def __init__(
        self,
        *,
        device_name: str = "ToupTek EFW 1",
        filter_wheel_available: bool = True,
        filter_slot: int = 1,
        filter_names: tuple[str, ...] | None = None,
        move_delay_s: float = 0.05,
    ) -> None:
        self._device_name = device_name
        #: Issue #34: libindi standard Filter Wheel Interface simulation.
        #: Issue #47: a commanded FILTER_SLOT now actually moves -- Busy
        #: immediately, Ok after `move_delay_s` (mirrors real hardware/the
        #: IndiFocuserAdapter fake's own Busy-then-Ok move pattern).
        self._filter_wheel_available = filter_wheel_available
        self._filter_slot = filter_slot
        self._filter_names = filter_names
        self._move_delay_s = move_delay_s
        self._pending_timers: list[threading.Timer] = []
        #: Simulates a driver-level focuser move rejection -- real libindi
        #: FocuserInterface semantics (verified against
        #: indifocuserinterface.cpp's source, see IndiFocuserAdapter's own
        #: docstring): MoveAbsFocuser()/MoveRelFocuser() can return
        #: IPS_ALERT, which the framework applies directly to
        #: ABS_FOCUS_POSITION/REL_FOCUS_POSITION's own vector state --
        #: unlike the mount's motion-switch rejection, the requested value
        #: is left in place, only the vector's state signals rejection.
        self._connected = False
        self._write_lock = threading.Lock()
        self._conn: socket.socket | None = None

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self._stop = threading.Event()
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="fake-indi-server-accept", daemon=True
        )

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        port: int = self._listener.getsockname()[1]
        return port

    def start(self) -> None:
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        for timer in self._pending_timers:
            timer.cancel()
        with self._write_lock:
            if self._conn is not None:
                with contextlib.suppress(OSError):
                    self._conn.shutdown(socket.SHUT_RDWR)
                self._conn.close()
                self._conn = None
        self._listener.close()
        self._accept_thread.join(timeout=2.0)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return  # listener closed
            # stop() may have run between accept() returning and here; it
            # would then have seen _conn=None and never closed this socket, so
            # the client would never observe EOF (a real CI flake in
            # TestConnectionLoss). Publishing _conn under the same lock stop()
            # takes makes exactly one side responsible for closing it.
            with self._write_lock:
                if self._stop.is_set():
                    conn.close()
                    return
                self._conn = conn
            parser = IncrementalIndiParser(self._on_client_element)
            try:
                while not self._stop.is_set():
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    parser.feed(chunk)
            except OSError:
                pass

    def _send(self, fragment: str) -> None:
        with self._write_lock:
            if self._conn is None:
                return
            with contextlib.suppress(OSError):
                self._conn.sendall(fragment.encode("utf-8"))

    def _send_switch_vector(self, name: str, state: str, elements: dict[str, bool]) -> None:
        children = "".join(
            f'<oneSwitch name="{xml_escape_attr(el)}">{"On" if val else "Off"}</oneSwitch>'
            for el, val in elements.items()
        )
        self._send(
            f'<setSwitchVector device="{xml_escape_attr(self._device_name)}" '
            f'name="{xml_escape_attr(name)}" state="{state}">{children}</setSwitchVector>'
        )

    def _def_switch_vector(self, name: str, state: str, elements: dict[str, bool]) -> None:
        children = "".join(
            f'<defSwitch name="{xml_escape_attr(el)}">{"On" if val else "Off"}</defSwitch>'
            for el, val in elements.items()
        )
        self._send(
            f'<defSwitchVector device="{xml_escape_attr(self._device_name)}" '
            f'name="{xml_escape_attr(name)}" state="{state}">{children}</defSwitchVector>'
        )

    def _send_number_vector(self, name: str, state: str, elements: dict[str, float]) -> None:
        children = "".join(
            f'<oneNumber name="{xml_escape_attr(el)}">{val}</oneNumber>'
            for el, val in elements.items()
        )
        self._send(
            f'<setNumberVector device="{xml_escape_attr(self._device_name)}" '
            f'name="{xml_escape_attr(name)}" state="{state}">{children}</setNumberVector>'
        )

    def _def_number_vector(
        self, name: str, state: str, elements: dict[str, float], *, max_value: float | None = None
    ) -> None:
        max_attr = f' max="{max_value}"' if max_value is not None else ""
        children = "".join(
            f'<defNumber name="{xml_escape_attr(el)}"{max_attr}>{val}</defNumber>'
            for el, val in elements.items()
        )
        self._send(
            f'<defNumberVector device="{xml_escape_attr(self._device_name)}" '
            f'name="{xml_escape_attr(name)}" state="{state}">{children}</defNumberVector>'
        )

    def _on_client_element(self, element: ParsedElement) -> None:
        if element.attrs.get("device") not in (self._device_name, None, ""):
            return
        if element.tag == "getProperties":
            self._handle_get_properties()
        elif element.tag == "newSwitchVector":
            self._handle_new_switch_vector(element.attrs.get("name", ""), element.children)
        elif element.tag == "newNumberVector":
            self._handle_new_number_vector(element.attrs.get("name", ""), element.children)

    def _def_text_vector(self, name: str, state: str, elements: dict[str, str]) -> None:
        children = "".join(
            f'<defText name="{xml_escape_attr(el)}">{val}</defText>' for el, val in elements.items()
        )
        self._send(
            f'<defTextVector device="{xml_escape_attr(self._device_name)}" '
            f'name="{xml_escape_attr(name)}" state="{state}">{children}</defTextVector>'
        )

    def _handle_get_properties(self) -> None:
        self._def_switch_vector(
            "CONNECTION", "Ok", {"CONNECT": self._connected, "DISCONNECT": not self._connected}
        )
        self._send_device_properties_if_connected()

    def _send_device_properties_if_connected(self) -> None:
        """Every per-device vector, gated on `self._connected` -- shared
        by `_handle_get_properties` and the `CONNECTION` switch handler
        below, which both need to (re-)announce the same set."""
        if not self._connected:
            return
        if self._filter_wheel_available:
            self._send_filter_wheel_properties()

    def _send_filter_wheel_properties(self) -> None:
        # FILTER_NAME sent BEFORE FILTER_SLOT deliberately -- connect()
        # (IndiFilterWheelAdapter) waits only on FILTER_SLOT, so it must
        # be the LAST vector sent here for that wait to also guarantee
        # FILTER_NAME has already been parsed and stored by the time it
        # unblocks. Sending FILTER_SLOT first (the original order) was a real,
        # load-sensitive race: status() could read FILTER_NAME before
        # the reader thread had parsed it (caught by a full-suite run,
        # not an isolated one -- see this file's own git history).
        if self._filter_names:
            self._def_text_vector(
                "FILTER_NAME",
                "Ok",
                {
                    f"FILTER_SLOT_NAME_{index}": name
                    for index, name in enumerate(self._filter_names, start=1)
                },
            )
        self._def_number_vector("FILTER_SLOT", "Ok", {"FILTER_SLOT_VALUE": self._filter_slot})

    def _handle_new_switch_vector(self, name: str, elements: dict[str, str]) -> None:
        if name == "CONNECTION":
            self._connected = elements.get("CONNECT") == "On"
            self._send_switch_vector(
                "CONNECTION", "Ok", {"CONNECT": self._connected, "DISCONNECT": not self._connected}
            )
            self._send_device_properties_if_connected()

    def _handle_new_number_vector(self, name: str, elements: dict[str, str]) -> None:
        """Issue #47: a commanded FILTER_SLOT actually moves -- Busy at once,
        Ok (at the new slot) after `move_delay_s`."""
        if name != "FILTER_SLOT" or not self._filter_wheel_available:
            return
        try:
            target = int(float(elements.get("FILTER_SLOT_VALUE", "")))
        except ValueError:
            return
        self._send_number_vector("FILTER_SLOT", "Busy", {"FILTER_SLOT_VALUE": target})

        def _finish() -> None:
            self._filter_slot = target
            self._send_number_vector("FILTER_SLOT", "Ok", {"FILTER_SLOT_VALUE": target})

        timer = threading.Timer(self._move_delay_s, _finish)
        timer.daemon = True
        self._pending_timers.append(timer)
        timer.start()
