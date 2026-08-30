"""Protocol-level tests for IndiClient, against a real (loopback)
FakeIndiServer — no indiserver/libindi install needed."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from astrotool_core.indi.client import IndiClient
from astrotool_core.testing.fake_indi_server import FakeIndiServer


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
        client.send_get_properties("LX200 OnStep")
        vector = client.wait_for_vector("LX200 OnStep", "CONNECTION", timeout_s=2.0)
        assert vector is not None
        assert vector.elements["CONNECT"] == "Off"
        assert vector.elements["DISCONNECT"] == "On"


class TestConnectSwitch:
    def test_connecting_reports_connection_ok(self, client: IndiClient) -> None:
        client.send_new_switch_vector("LX200 OnStep", "CONNECTION", {"CONNECT": True})
        vector = client.wait_for_vector(
            "LX200 OnStep", "CONNECTION", timeout_s=2.0, predicate=lambda v: v.state == "Ok"
        )
        assert vector is not None
        assert vector.elements["CONNECT"] == "On"

    def test_connecting_defines_focuser_vectors(self, client: IndiClient) -> None:
        client.send_new_switch_vector("LX200 OnStep", "CONNECTION", {"CONNECT": True})
        vector = client.wait_for_vector("LX200 OnStep", "ABS_FOCUS_POSITION", timeout_s=2.0)
        assert vector is not None
        assert vector.elements["FOCUS_ABSOLUTE_POSITION"] == "5000"

    def test_a_server_with_no_focuser_never_defines_focuser_vectors(self) -> None:
        fake = FakeIndiServer(focuser_available=False)
        fake.start()
        try:
            c = IndiClient(fake.host, fake.port)
            c.connect()
            try:
                c.send_new_switch_vector("LX200 OnStep", "CONNECTION", {"CONNECT": True})
                vector = c.wait_for_vector("LX200 OnStep", "ABS_FOCUS_POSITION", timeout_s=0.5)
                assert vector is None
            finally:
                c.close()
        finally:
            fake.stop()


class TestRelativeMove:
    def test_moving_outward_increases_position(self, client: IndiClient) -> None:
        client.send_new_switch_vector("LX200 OnStep", "CONNECTION", {"CONNECT": True})
        client.wait_for_vector("LX200 OnStep", "ABS_FOCUS_POSITION", timeout_s=2.0)

        client.send_new_switch_vector(
            "LX200 OnStep", "FOCUS_MOTION", {"FOCUS_INWARD": False, "FOCUS_OUTWARD": True}
        )
        client.wait_for_vector(
            "LX200 OnStep", "FOCUS_MOTION", timeout_s=2.0, predicate=lambda v: v.state == "Ok"
        )
        client.send_new_number_vector(
            "LX200 OnStep", "REL_FOCUS_POSITION", {"FOCUS_RELATIVE_POSITION": 10}
        )
        vector = client.wait_for_vector(
            "LX200 OnStep",
            "ABS_FOCUS_POSITION",
            timeout_s=2.0,
            predicate=lambda v: v.state == "Ok" and v.elements["FOCUS_ABSOLUTE_POSITION"] == "5010",
        )
        assert vector is not None

    def test_move_reports_busy_before_ok(self, client: IndiClient) -> None:
        client.send_new_switch_vector("LX200 OnStep", "CONNECTION", {"CONNECT": True})
        client.wait_for_vector("LX200 OnStep", "ABS_FOCUS_POSITION", timeout_s=2.0)
        client.send_new_number_vector(
            "LX200 OnStep", "REL_FOCUS_POSITION", {"FOCUS_RELATIVE_POSITION": 10}
        )
        busy = client.wait_for_vector(
            "LX200 OnStep",
            "ABS_FOCUS_POSITION",
            timeout_s=2.0,
            predicate=lambda v: v.state == "Busy",
        )
        assert busy is not None


class TestAbort:
    def test_abort_returns_focus_motion_to_ok(self, client: IndiClient) -> None:
        client.send_new_switch_vector("LX200 OnStep", "CONNECTION", {"CONNECT": True})
        client.wait_for_vector("LX200 OnStep", "ABS_FOCUS_POSITION", timeout_s=2.0)
        client.send_new_switch_vector("LX200 OnStep", "FOCUS_ABORT_MOTION", {"ABORT": True})
        vector = client.wait_for_vector(
            "LX200 OnStep",
            "FOCUS_ABORT_MOTION",
            timeout_s=2.0,
            predicate=lambda v: v.elements.get("ABORT") == "Off",
        )
        assert vector is not None


class TestWaitForVectorTimeout:
    def test_returns_none_if_never_defined(self, client: IndiClient) -> None:
        result = client.wait_for_vector("LX200 OnStep", "NEVER_DEFINED", timeout_s=0.2)
        assert result is None


class TestNotConnected:
    def test_send_before_connect_raises_connection_error(self) -> None:
        c = IndiClient("127.0.0.1", 1)  # never connected
        with pytest.raises(ConnectionError):
            c.send_get_properties()

    def test_get_vector_before_connect_returns_none(self) -> None:
        c = IndiClient("127.0.0.1", 1)
        assert c.get_vector("LX200 OnStep", "CONNECTION") is None

    def test_connect_to_a_closed_port_raises_connection_error(self) -> None:
        fake = FakeIndiServer()
        fake.start()
        port = fake.port
        fake.stop()
        c = IndiClient("127.0.0.1", port)
        with pytest.raises(ConnectionError):
            c.connect()
