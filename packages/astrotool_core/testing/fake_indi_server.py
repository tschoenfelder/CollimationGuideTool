"""FakeIndiServer — a real local TCP server speaking just enough of the
INDI wire protocol to drive `IndiClient`/`IndiFocuserAdapter` end-to-end
in tests, on any platform (no indiserver/libindi install needed).

Simulates one device (default `"LX200 OnStep"`, matching the real rig's
INDI OnStep driver — see `IndiFocuserAdapter`'s docstring) with a
`CONNECTION` switch vector always present, and — only after a simulated
connect, mirroring the real driver's own "interfaces probed after
connect" behavior — the standard libindi Focuser Interface vectors
(`ABS_FOCUS_POSITION`, `FOCUS_MAX`, `FOCUS_MOTION`, `REL_FOCUS_POSITION`,
`FOCUS_ABORT_MOTION`), unless `focuser_available=False` (simulating "no
focuser hardware detected"), and the standard libindi Telescope
Interface's park/tracking vectors (`TELESCOPE_PARK`,
`TELESCOPE_TRACK_STATE` — see `IndiMountParkAdapter`), unless
`mount_available=False`.

Ported in spirit from `fake_touptek.py`'s configurable-failure-mode
style: construct with the failure mode you want, `start()`, point an
`IndiClient`/`IndiFocuserAdapter` at `.host`/`.port`, `stop()` when done.
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
        device_name: str = "LX200 OnStep",
        focuser_available: bool = True,
        start_position: int = 5000,
        max_position: int = 50000,
        move_delay_s: float = 0.05,
        mount_available: bool = True,
        start_parked: bool = True,
        park_delay_s: float = 0.05,
    ) -> None:
        self._device_name = device_name
        self._focuser_available = focuser_available
        self._position = start_position
        self._max_position = max_position
        self._move_delay_s = move_delay_s
        self._mount_available = mount_available
        self._parked = start_parked
        self._tracking = False
        self._park_delay_s = park_delay_s

        self._connected = False
        self._direction_outward = True
        self._write_lock = threading.Lock()
        self._conn: socket.socket | None = None
        self._pending_timers: list[threading.Timer] = []

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

    def _handle_get_properties(self) -> None:
        self._def_switch_vector(
            "CONNECTION", "Ok", {"CONNECT": self._connected, "DISCONNECT": not self._connected}
        )
        if self._connected and self._focuser_available:
            self._send_focuser_properties()
        if self._connected and self._mount_available:
            self._send_mount_properties()

    def _send_mount_properties(self) -> None:
        self._def_switch_vector(
            "TELESCOPE_PARK", "Ok", {"PARK": self._parked, "UNPARK": not self._parked}
        )
        self._def_switch_vector(
            "TELESCOPE_TRACK_STATE",
            "Ok",
            {"TRACK_ON": self._tracking, "TRACK_OFF": not self._tracking},
        )

    def _send_focuser_properties(self) -> None:
        self._def_number_vector(
            "ABS_FOCUS_POSITION",
            "Ok",
            {"FOCUS_ABSOLUTE_POSITION": self._position},
            max_value=self._max_position,
        )
        self._def_number_vector("FOCUS_MAX", "Ok", {"FOCUS_MAX_VALUE": self._max_position})
        self._def_switch_vector(
            "FOCUS_MOTION",
            "Ok",
            {"FOCUS_INWARD": not self._direction_outward, "FOCUS_OUTWARD": self._direction_outward},
        )
        self._def_switch_vector("FOCUS_ABORT_MOTION", "Ok", {"ABORT": False})

    def _handle_new_switch_vector(self, name: str, elements: dict[str, str]) -> None:
        if name == "CONNECTION":
            self._connected = elements.get("CONNECT") == "On"
            self._send_switch_vector(
                "CONNECTION", "Ok", {"CONNECT": self._connected, "DISCONNECT": not self._connected}
            )
            if self._connected and self._focuser_available:
                self._send_focuser_properties()
            if self._connected and self._mount_available:
                self._send_mount_properties()
        elif name == "TELESCOPE_PARK":
            if elements.get("UNPARK") == "On":
                self._set_parked(False)
            elif elements.get("PARK") == "On":
                self._set_parked(True)
        elif name == "TELESCOPE_TRACK_STATE":
            self._tracking = elements.get("TRACK_ON") == "On"
            self._send_switch_vector(
                "TELESCOPE_TRACK_STATE",
                "Ok",
                {"TRACK_ON": self._tracking, "TRACK_OFF": not self._tracking},
            )
        elif name == "FOCUS_MOTION":
            self._direction_outward = elements.get("FOCUS_OUTWARD") == "On"
            self._send_switch_vector(
                "FOCUS_MOTION",
                "Ok",
                {
                    "FOCUS_INWARD": not self._direction_outward,
                    "FOCUS_OUTWARD": self._direction_outward,
                },
            )
        elif name == "FOCUS_ABORT_MOTION":
            self._cancel_pending_timers()
            self._send_number_vector(
                "ABS_FOCUS_POSITION", "Ok", {"FOCUS_ABSOLUTE_POSITION": self._position}
            )
            self._send_switch_vector("FOCUS_ABORT_MOTION", "Ok", {"ABORT": False})

    def _handle_new_number_vector(self, name: str, elements: dict[str, str]) -> None:
        if name == "REL_FOCUS_POSITION":
            steps = float(elements.get("FOCUS_RELATIVE_POSITION", "0"))
            delta = steps if self._direction_outward else -steps
            self._move_to(self._position + delta)
        elif name == "ABS_FOCUS_POSITION":
            target = float(elements.get("FOCUS_ABSOLUTE_POSITION", str(self._position)))
            self._move_to(target)

    def _move_to(self, target: float) -> None:
        clamped = max(0, min(self._max_position, int(round(target))))
        self._send_number_vector("ABS_FOCUS_POSITION", "Busy", {"FOCUS_ABSOLUTE_POSITION": clamped})

        def _finish() -> None:
            self._position = clamped
            self._send_number_vector(
                "ABS_FOCUS_POSITION", "Ok", {"FOCUS_ABSOLUTE_POSITION": clamped}
            )

        timer = threading.Timer(self._move_delay_s, _finish)
        timer.daemon = True
        self._pending_timers.append(timer)
        timer.start()

    def _set_parked(self, parked: bool) -> None:
        self._send_switch_vector(
            "TELESCOPE_PARK", "Busy", {"PARK": parked, "UNPARK": not parked}
        )

        def _finish() -> None:
            self._parked = parked
            self._send_switch_vector(
                "TELESCOPE_PARK", "Ok", {"PARK": self._parked, "UNPARK": not self._parked}
            )

        timer = threading.Timer(self._park_delay_s, _finish)
        timer.daemon = True
        self._pending_timers.append(timer)
        timer.start()

    def _cancel_pending_timers(self) -> None:
        for timer in self._pending_timers:
            timer.cancel()
        self._pending_timers = []
