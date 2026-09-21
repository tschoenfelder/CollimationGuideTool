"""Protocol-level tests for IndiClient, against a real (loopback)
FakeIndiServer — no indiserver/libindi install needed."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from astrotool_core.indi.client import IndiClient
from astrotool_core.testing.fake_indi_server import FakeIndiServer

_DEVICE = "ToupTek EFW 1"


@pytest.fixture
def server() -> Iterator[FakeIndiServer]:
    fake = FakeIndiServer()
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def client(server: FakeIndiServer) -> Iterator[IndiClient]:
    c = IndiClient(server.host, server.port)
    c.connect()
    try:
        yield c
    finally:
        c.close()


class TestGetProperties:
    def test_requesting_properties_defines_the_connection_vector(self, client: IndiClient) -> None:
        client.send_get_properties(_DEVICE)
        vector = client.wait_for_vector(_DEVICE, "CONNECTION", timeout_s=2.0)
        assert vector is not None
        assert vector.elements["CONNECT"] == "Off"
        assert vector.elements["DISCONNECT"] == "On"


class TestConnectSwitch:
    def test_connecting_reports_connection_ok(self, client: IndiClient) -> None:
        client.send_new_switch_vector(_DEVICE, "CONNECTION", {"CONNECT": True})
        vector = client.wait_for_vector(
            _DEVICE, "CONNECTION", timeout_s=2.0, predicate=lambda v: v.state == "Ok"
        )
        assert vector is not None
        assert vector.elements["CONNECT"] == "On"

    def test_connecting_defines_the_filter_wheel_vectors(self, client: IndiClient) -> None:
        client.send_new_switch_vector(_DEVICE, "CONNECTION", {"CONNECT": True})
        vector = client.wait_for_vector(_DEVICE, "FILTER_SLOT", timeout_s=2.0)
        assert vector is not None
        assert vector.elements["FILTER_SLOT_VALUE"] == "1"

    def test_a_device_without_a_filter_wheel_never_defines_its_vectors(self) -> None:
        fake = FakeIndiServer(filter_wheel_available=False)
        fake.start()
        try:
            c = IndiClient(fake.host, fake.port)
            c.connect()
            try:
                c.send_new_switch_vector(_DEVICE, "CONNECTION", {"CONNECT": True})
                assert c.wait_for_vector(_DEVICE, "FILTER_SLOT", timeout_s=0.5) is None
            finally:
                c.close()
        finally:
            fake.stop()


class TestWaitForVectorTimeout:
    def test_returns_none_if_never_defined(self, client: IndiClient) -> None:
        result = client.wait_for_vector(_DEVICE, "NEVER_DEFINED", timeout_s=0.2)
        assert result is None


class TestConnectionLoss:
    """Real incident b6d3384b: an indiserver connection drop turned every
    subsequent send into an unhandled `BrokenPipeError` and
    left `wait_for_vector` blocking out its full timeout on the dead
    socket (minute-long "test move" UI freezes)."""

    def test_a_write_error_becomes_connection_error_and_marks_disconnected(
        self, client: IndiClient
    ) -> None:
        class _DeadSocket:
            def sendall(self, _data: bytes) -> None:
                raise BrokenPipeError(32, "Broken pipe")

            def close(self) -> None:
                pass

        client._sock = _DeadSocket()  # type: ignore[assignment]

        with pytest.raises(ConnectionError):
            client.send_new_switch_vector(_DEVICE, "CONNECTION", {"CONNECT": True})
        assert not client.is_connected

    def test_a_dropped_server_is_detected_and_wait_for_vector_returns_promptly(
        self, server: FakeIndiServer
    ) -> None:
        c = IndiClient(server.host, server.port)
        c.connect()
        assert c.is_connected
        server.stop()  # kill the server side of the connection

        deadline = time.monotonic() + 3.0
        while c.is_connected and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not c.is_connected  # the read loop noticed the EOF

        # A send after the drop raises ConnectionError, never a bare
        # BrokenPipeError, and wait_for_vector returns at once rather than
        # blocking its whole 5s timeout on the dead socket.
        with pytest.raises(ConnectionError):
            c.send_new_switch_vector(_DEVICE, "CONNECTION", {"CONNECT": True})
        started = time.monotonic()
        assert c.wait_for_vector(_DEVICE, "NEVER_DEFINED", timeout_s=5.0) is None
        assert time.monotonic() - started < 1.0
        c.close()


class TestNotConnected:
    def test_send_before_connect_raises_connection_error(self) -> None:
        c = IndiClient("127.0.0.1", 1)  # never connected
        with pytest.raises(ConnectionError):
            c.send_get_properties()

    def test_get_vector_before_connect_returns_none(self) -> None:
        c = IndiClient("127.0.0.1", 1)
        assert c.get_vector(_DEVICE, "CONNECTION") is None

    def test_connect_to_nothing_listening_raises_connection_error(self) -> None:
        # Port 1 is a reserved system port essentially never listened on —
        # unlike "start a FakeIndiServer, stop it, reconnect to its freed
        # ephemeral port" (tried first here), which is a genuine race: the
        # OS can and does hand that just-freed port straight back out to
        # the *next* ephemeral bind (e.g. another test's own FakeIndiServer)
        # before this test's connection attempt lands, seen flaking on CI.
        c = IndiClient("127.0.0.1", 1)
        with pytest.raises(ConnectionError):
            c.connect()
