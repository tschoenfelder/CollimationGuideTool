"""Scenario simulator for an INDI filter wheel behind `IndiFilterWheelAdapter`
(issue #51).

Two pieces, both on an injected `astrotool_core.timing.Clock` (a `FakeClock`
in tests) -- no socket, no reader thread, no real sleeps:

- `SimulatedIndiClient` is the production `IndiClient` with only its socket
  I/O and its wait replaced: the real XML emission (`send_*`), the real
  incremental parser and the real vector cache (`_on_element`, `get_vector`)
  all run. Outgoing fragments go to a simulated device; the device's replies
  are fed to the client's own parser. `wait_for_vector` keeps the production
  contract (None on timeout or when the connection is gone, deadline expired
  at `now >= deadline`) but waits on the clock, so a 10 s connect timeout
  costs no real time and a device reply scheduled during the wait arrives.
- `SimulatedIndiFilterWheel` is the driver: libindi's Filter Wheel Interface
  (`CONNECTION`, `FILTER_NAME`, `FILTER_SLOT`, the wheel vectors defined only
  after a connect), with configurable slot names/count, device name, travel
  time per slot, Busy forever, reply latency (property update delay), an
  out-of-range slot answered with `Alert` (libindi `FilterInterface`), a
  connection drop at any moment, and a `getProperties` re-announce that
  reports the *current* state -- Busy while moving (the loopback
  `FakeIndiServer` re-announces `Ok`, which is not what a driver does).
  While Busy, FILTER_SLOT keeps the OLD slot value by default: libindi's
  `FilterInterface::processNumber` (libs/indibase/indifilterinterface.cpp,
  indilib/indi master, read 2026-10-01) only stores the request in
  `TargetFilter`, sets IPS_BUSY and applies; the value changes in
  `SelectFilterDone(f)` together with IPS_OK. A driver may override this, so
  `busy_reports_target=True` models one that echoes the target (as
  `FakeIndiServer` does) -- unverified for the rig's ToupTek EFW driver.

`make_simulated_filter_wheel_adapter` wires a production
`IndiFilterWheelAdapter` to the simulated client. The adapter builds its own
`IndiClient` and has no injection seam, so the helper swaps the private
`_client` after construction (a test-side seam; recorded as a #51 dependency:
give the adapter a `client_factory`, as `OnStepConnection` has). The adapter's
own 2 s property-refresh throttle still reads the real `time.monotonic()`; the
simulator answers every refresh with the current state, so how many refreshes
happen never changes what a test observes.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from astrotool_core.config.device_defaults import INDI_PORT
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.indi._protocol import IncrementalIndiParser, ParsedElement, xml_escape_attr
from astrotool_core.indi.client import IndiClient, VectorState
from astrotool_core.timing import Clock, Deadline


class _SimulatedSocket:
    """Stands in for the client's socket object; nothing is ever sent on it."""

    def shutdown(self, _how: int) -> None:
        pass

    def close(self) -> None:
        pass


class SimulatedIndiDevice:
    """What a simulated INDI driver must offer `SimulatedIndiClient`."""

    def attach(self, client: SimulatedIndiClient) -> None:
        raise NotImplementedError

    def receive(self, fragment: str) -> None:
        raise NotImplementedError


class SimulatedIndiClient(IndiClient):
    """Production `IndiClient` minus the TCP socket, waiting on `clock`."""

    def __init__(
        self,
        device: SimulatedIndiDevice,
        *,
        clock: Clock,
        server_reachable: bool = True,
        poll_interval_s: float = 0.05,
    ) -> None:
        super().__init__("simulated-indiserver", INDI_PORT)
        self._clock = clock
        self._device = device
        self._server_reachable = server_reachable
        self._poll_interval_s = poll_interval_s
        #: Every fragment the production code sent, in order.
        self.sent: list[str] = []
        device.attach(self)

    def connect(self) -> None:
        if not self._server_reachable:
            # Same shape as the real connect's failure (OSError -> ConnectionError).
            raise ConnectionError(
                f"IndiClient: could not connect to indiserver at {self._host}:{self._port}: "
                "[simulated] connection refused"
            )
        self._sock = cast(socket.socket, _SimulatedSocket())
        self._stop.clear()

    def _send(self, fragment: str) -> None:
        if self._sock is None:
            raise ConnectionError("IndiClient: not connected")
        self.sent.append(fragment)
        self._device.receive(fragment)

    def deliver(self, xml: str) -> None:
        """A device reply arriving on the wire: the real parser handles it."""
        if self._sock is None:
            return  # the connection is gone; nothing reaches the client
        self._parser.feed(xml.encode("utf-8"))

    def drop_connection(self) -> None:
        """The indiserver connection is lost (the read loop's EOF path)."""
        self._mark_disconnected()

    def wait_for_vector(
        self,
        device: str,
        name: str,
        timeout_s: float,
        predicate: Callable[[VectorState], bool] | None = None,
    ) -> VectorState | None:
        deadline = Deadline.after(timeout_s, clock=self._clock)
        while True:
            if self._sock is None:
                return None
            vector = self.get_vector(device, name)
            if vector is not None and (predicate is None or predicate(vector)):
                return vector
            if deadline.expired():
                return None
            self._clock.sleep(min(self._poll_interval_s, max(deadline.remaining(), 0.0)))


@dataclass(frozen=True)
class FilterWheelScenario:
    """One filter wheel configuration. `device_name` is deliberately required:
    the authoritative default lives in `filter_wheel.config` (#55)."""

    device_name: str
    slot_names: tuple[str, ...] = ("L", "R", "G", "B", "Ha", "OIII", "SII", "NONE")
    initial_slot: int = 1
    #: False: the driver connects but never defines the wheel vectors.
    wheel_present: bool = True
    #: False: the driver never confirms CONNECTION (connect timeout).
    confirms_connection: bool = True
    #: False: nothing listens on the indiserver port at all.
    server_reachable: bool = True
    seconds_per_slot: float = 0.5
    #: A commanded move never leaves Busy.
    busy_forever: bool = False
    #: Every reply reaches the client this long after the command (fake time).
    reply_latency_s: float = 0.0
    #: False: no FILTER_NAME vector (names only from configuration).
    publishes_names: bool = True
    #: True: FILTER_SLOT shows the target while Busy (libindi keeps the old slot).
    busy_reports_target: bool = False


class SimulatedIndiFilterWheel(SimulatedIndiDevice):
    def __init__(self, scenario: FilterWheelScenario, *, clock: Clock) -> None:
        self.scenario = scenario
        self._clock = clock
        self._client: SimulatedIndiClient | None = None
        self._parser = IncrementalIndiParser(self._on_element)
        self.connected = False
        self.slot = scenario.initial_slot
        self.state = "Ok"
        self._target = scenario.initial_slot
        self._generation = 0
        #: Every slot the client commanded, in order (also rejected ones).
        self.commanded_slots: list[int] = []

    # -- wiring -------------------------------------------------------------
    def attach(self, client: SimulatedIndiClient) -> None:
        self._client = client

    def receive(self, fragment: str) -> None:
        self._parser.feed(fragment.encode("utf-8"))

    @property
    def slot_count(self) -> int:
        return len(self.scenario.slot_names)

    # -- scenario controls ----------------------------------------------------
    def drop_connection(self) -> None:
        """indiserver goes away now (e.g. scheduled mid-move with `call_at`)."""
        if self._client is not None:
            self._client.drop_connection()

    def external_move(self, slot: int) -> None:
        """The wheel changes position with nothing in this app prompting it
        (hand control, another client); the driver pushes the new state."""
        self.slot = self._target = slot
        self.state = "Ok"
        self._send_slot("set")

    # -- replies ---------------------------------------------------------------
    def _reply(self, xml: str) -> None:
        client = self._client
        if client is None:
            return
        latency = self.scenario.reply_latency_s
        if latency <= 0:
            client.deliver(xml)
            return
        call_later = getattr(self._clock, "call_later", None)
        if call_later is None:
            client.deliver(xml)
        else:
            call_later(latency, lambda: client.deliver(xml))

    def _vector(self, kind: str, tag: str, name: str, state: str, children: str) -> None:
        device = xml_escape_attr(self.scenario.device_name)
        self._reply(
            f'<{kind}{tag}Vector device="{device}" name="{name}" state="{state}">'
            f"{children}</{kind}{tag}Vector>"
        )

    def _send_connection(self, kind: str) -> None:
        on = self.connected
        children = (
            f'<{kind}Switch name="CONNECT">{"On" if on else "Off"}</{kind}Switch>'
            f'<{kind}Switch name="DISCONNECT">{"Off" if on else "On"}</{kind}Switch>'
        )
        self._vector(kind, "Switch", "CONNECTION", "Ok", children)

    def _send_slot(self, kind: str) -> None:
        busy_target = self.state == "Busy" and self.scenario.busy_reports_target
        value = self._target if busy_target else self.slot
        children = f'<{kind}Number name="FILTER_SLOT_VALUE">{value}</{kind}Number>'
        self._vector(kind, "Number", "FILTER_SLOT", self.state, children)

    def _send_wheel_definitions(self) -> None:
        if not (self.connected and self.scenario.wheel_present):
            return
        # FILTER_NAME before FILTER_SLOT: the adapter's connect waits only on
        # FILTER_SLOT (same ordering rule as FakeIndiServer).
        if self.scenario.publishes_names:
            children = "".join(
                f'<defText name="FILTER_SLOT_NAME_{i}">{xml_escape_attr(n)}</defText>'
                for i, n in enumerate(self.scenario.slot_names, start=1)
            )
            self._vector("def", "Text", "FILTER_NAME", "Ok", children)
        self._send_slot("def")

    # -- driver ----------------------------------------------------------------
    def _on_element(self, element: ParsedElement) -> None:
        if element.attrs.get("device") not in (self.scenario.device_name, None, ""):
            return  # addressed to another device: no driver of that name here
        name = element.attrs.get("name", "")
        if element.tag == "getProperties":
            self._send_connection("def")
            self._send_wheel_definitions()
        elif element.tag == "newSwitchVector" and name == "CONNECTION":
            self._on_connection(element.children)
        elif element.tag == "newNumberVector" and name == "FILTER_SLOT":
            self._on_slot_command(element.children)

    def _on_connection(self, elements: dict[str, str]) -> None:
        if not self.scenario.confirms_connection:
            return
        self.connected = elements.get("CONNECT") == "On"
        self._send_connection("set")
        self._send_wheel_definitions()

    def _on_slot_command(self, elements: dict[str, str]) -> None:
        if not (self.connected and self.scenario.wheel_present):
            return
        try:
            target = int(float(elements.get("FILTER_SLOT_VALUE", "")))
        except ValueError:
            return
        self.commanded_slots.append(target)
        if not 1 <= target <= self.slot_count:
            # libindi FilterInterface: out of [min, max] -> Alert, value unchanged.
            self.state = "Alert"
            self._send_slot("set")
            return
        start = self._target if self.state == "Busy" else self.slot
        self._target = target
        self.state = "Busy"
        self._generation += 1
        generation = self._generation
        self._send_slot("set")
        if self.scenario.busy_forever:
            return
        travel_s = abs(target - start) * self.scenario.seconds_per_slot
        call_later: Any = getattr(self._clock, "call_later", None)
        if call_later is None:
            self._finish(generation)
        else:
            call_later(travel_s, lambda: self._finish(generation))

    def _finish(self, generation: int) -> None:
        if generation != self._generation or self.state != "Busy":
            return
        self.slot = self._target
        self.state = "Ok"
        self._send_slot("set")


def make_simulated_filter_wheel_adapter(
    scenario: FilterWheelScenario,
    *,
    clock: Clock,
    device_name: str | None = None,
    connect_timeout_s: float = 10.0,
    filter_names: dict[int, str] | None = None,
    names_override: bool = False,
) -> tuple[IndiFilterWheelAdapter, SimulatedIndiFilterWheel]:
    """A production `IndiFilterWheelAdapter` talking to a simulated wheel.
    `device_name` is what the *adapter* is configured with (default: the
    simulated driver's own name); pass a different one to reproduce a wrong
    configured device name (#47)."""
    wheel = SimulatedIndiFilterWheel(scenario, clock=clock)
    adapter = IndiFilterWheelAdapter(
        "simulated-indiserver",
        INDI_PORT,
        device_name or scenario.device_name,
        connect_timeout_s=connect_timeout_s,
        filter_names=filter_names,
        names_override=names_override,
    )
    # Test-side seam: the adapter has no client injection (see module docstring).
    adapter._client = SimulatedIndiClient(
        wheel, clock=clock, server_reachable=scenario.server_reachable
    )
    return adapter, wheel
