"""IndiClient — a small, generic INDI protocol client over a raw TCP
socket to a running indiserver.

Unlike `astrotool_core/mount/indi_adapter.py` (which, despite its name,
speaks OnStep's native serial protocol directly — see that module's own
docstring), this one genuinely speaks the INDI wire protocol: XML
fragments over TCP, no `pyindi-client`/SWIG/libindi dependency. See
`astrotool_core.indi._protocol` for why a hand-rolled incremental parser
is needed at all (INDI's "stream of top-level elements, no wrapping
root" quirk) and why that's a reasonable scope for this project to
hand-roll rather than add a new dependency for.

Generic on purpose: this client knows nothing about focusers specifically
— it tracks whatever "vectors" (INDI's term for a named group of
properties, e.g. `ABS_FOCUS_POSITION`) a device defines/updates, and lets
a caller wait for or read the latest known state of any of them by
name. `astrotool_core/focus/indi_focuser_adapter.py` is the focuser-
specific layer built on top of this.
"""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from astrotool_core.indi._protocol import IncrementalIndiParser, ParsedElement, xml_escape_attr

_log = logging.getLogger(__name__)

_DEFAULT_PORT = 7624
_CONNECT_TIMEOUT_S = 5.0

#: Top-level element tags this client keeps as named "vectors" — a driver's
#: def*Vector (initial definition) and set*Vector (later update) alike.
#: message/delProperty/getProperties are seen but not tracked as vectors.
_TRACKED_VECTOR_TAGS = frozenset(
    {
        "defSwitchVector",
        "setSwitchVector",
        "defNumberVector",
        "setNumberVector",
        "defTextVector",
        "setTextVector",
    }
)


@dataclass(frozen=True)
class VectorState:
    """The latest known state of one INDI property vector."""

    state: str  # "Idle" | "Ok" | "Busy" | "Alert"
    elements: dict[str, str] = field(default_factory=dict)


class IndiClient:
    """One TCP connection to indiserver, tracking every vector it has
    seen for later synchronous `get_vector`/`wait_for_vector` reads.

    Not itself a `FocuserPort`/`MountPort` — see `IndiFocuserAdapter` for
    that. Safe to call `send_*`/`get_vector` before `connect()` or after
    `close()`: `send_*` raises `ConnectionError`, `get_vector` returns
    `None`, matching this project's "hardware adapters never crash a
    caller that forgot to check connection state first" convention.
    """

    def __init__(self, host: str, port: int = _DEFAULT_PORT) -> None:
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._vectors: dict[tuple[str, str], VectorState] = {}
        self._parser = IncrementalIndiParser(self._on_element)

    @property
    def is_connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=_CONNECT_TIMEOUT_S
            )
        except OSError as exc:
            self._sock = None
            raise ConnectionError(
                f"IndiClient: could not connect to indiserver at {self._host}:{self._port}: {exc}"
            ) from exc
        self._sock.settimeout(0.5)  # let _read_loop notice self._stop promptly
        self._stop.clear()
        self._reader_thread = threading.Thread(
            target=self._read_loop, name="indi-client-reader", daemon=True
        )
        self._reader_thread.start()

    def close(self) -> None:
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        thread, self._reader_thread = self._reader_thread, None
        if thread is not None:
            thread.join(timeout=2.0)

    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                break
            if not chunk:
                break
            self._parser.feed(chunk)

    def _on_element(self, element: ParsedElement) -> None:
        if element.tag not in _TRACKED_VECTOR_TAGS:
            return  # message/delProperty/etc. — nothing this client tracks
        device = element.attrs.get("device", "")
        name = element.attrs.get("name", "")
        state = element.attrs.get("state", "Idle")
        with self._cond:
            self._vectors[(device, name)] = VectorState(state=state, elements=element.children)
            self._cond.notify_all()

    def _send(self, fragment: str) -> None:
        sock = self._sock
        if sock is None:
            raise ConnectionError("IndiClient: not connected")
        sock.sendall(fragment.encode("utf-8"))

    def send_get_properties(self, device: str | None = None) -> None:
        attr = f' device="{xml_escape_attr(device)}"' if device else ""
        self._send(f'<getProperties version="1.7"{attr}/>')

    def send_new_switch_vector(self, device: str, name: str, elements: dict[str, bool]) -> None:
        children = "".join(
            f'<oneSwitch name="{xml_escape_attr(element)}">{"On" if value else "Off"}</oneSwitch>'
            for element, value in elements.items()
        )
        self._send(
            f'<newSwitchVector device="{xml_escape_attr(device)}" '
            f'name="{xml_escape_attr(name)}">{children}</newSwitchVector>'
        )

    def send_new_number_vector(self, device: str, name: str, elements: dict[str, float]) -> None:
        children = "".join(
            f'<oneNumber name="{xml_escape_attr(element)}">{value}</oneNumber>'
            for element, value in elements.items()
        )
        self._send(
            f'<newNumberVector device="{xml_escape_attr(device)}" '
            f'name="{xml_escape_attr(name)}">{children}</newNumberVector>'
        )

    def get_vector(self, device: str, name: str) -> VectorState | None:
        with self._cond:
            return self._vectors.get((device, name))

    def wait_for_vector(
        self,
        device: str,
        name: str,
        timeout_s: float,
        predicate: Callable[[VectorState], bool] | None = None,
    ) -> VectorState | None:
        """Block until `(device, name)` has a state satisfying `predicate`
        (default: any state at all), or `timeout_s` elapses. Returns the
        matching `VectorState`, or `None` on timeout."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                vector = self._vectors.get((device, name))
                if vector is not None and (predicate is None or predicate(vector)):
                    return vector
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)
