"""Full FilterWheelPort behavior for IndiFilterWheelAdapter against a
real (loopback) FakeIndiServer -- issue #34."""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.indi.client import VectorState
from astrotool_core.testing.fake_indi_server import FakeIndiServer

_DEVICE_NAME = "Filter Wheel"


@pytest.fixture
def server() -> Iterator[FakeIndiServer]:
    fake = FakeIndiServer(
        device_name=_DEVICE_NAME,
        filter_slot=2,
        filter_names=("Luminance", "Red", "Green", "Blue", "OIII"),
    )
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def filter_wheel(server: FakeIndiServer) -> Iterator[IndiFilterWheelAdapter]:
    adapter = IndiFilterWheelAdapter(server.host, server.port, _DEVICE_NAME, connect_timeout_s=2.0)
    yield adapter
    adapter.disconnect()


class TestConnectAndStatus:
    def test_connect_reports_available_slot_and_name(
        self, filter_wheel: IndiFilterWheelAdapter
    ) -> None:
        filter_wheel.connect()
        assert filter_wheel.is_available is True
        status = filter_wheel.status()
        assert status.available is True
        assert status.current_slot == 2
        assert status.filter_name == "Red"
        assert status.moving is False
        assert status.reason is None

    def test_no_filter_wheel_hardware_detected(self) -> None:
        fake = FakeIndiServer(device_name=_DEVICE_NAME, filter_wheel_available=False)
        fake.start()
        adapter = IndiFilterWheelAdapter(fake.host, fake.port, _DEVICE_NAME, connect_timeout_s=2.0)
        try:
            adapter.connect()
            assert adapter.is_available is False
            status = adapter.status()
            assert status.available is False
            assert status.current_slot is None
            assert status.reason == "no filter wheel detected"
        finally:
            adapter.disconnect()
            fake.stop()

    def test_no_filter_names_configured_leaves_name_none(self) -> None:
        fake = FakeIndiServer(device_name=_DEVICE_NAME, filter_slot=1, filter_names=None)
        fake.start()
        adapter = IndiFilterWheelAdapter(fake.host, fake.port, _DEVICE_NAME, connect_timeout_s=2.0)
        try:
            adapter.connect()
            status = adapter.status()
            assert status.available is True
            assert status.current_slot == 1
            assert status.filter_name is None
        finally:
            adapter.disconnect()
            fake.stop()


class TestNotConnected:
    def test_is_available_is_false(self) -> None:
        adapter = IndiFilterWheelAdapter("127.0.0.1", 1, _DEVICE_NAME)
        assert adapter.is_available is False

    def test_status_reports_not_connected(self) -> None:
        adapter = IndiFilterWheelAdapter("127.0.0.1", 1, _DEVICE_NAME)
        status = adapter.status()
        assert status.available is False
        assert status.current_slot is None
        assert status.reason == "not connected"


class TestDisconnect:
    def test_disconnect_makes_the_filter_wheel_unavailable(
        self, filter_wheel: IndiFilterWheelAdapter
    ) -> None:
        filter_wheel.connect()
        filter_wheel.disconnect()
        assert filter_wheel.is_available is False


class TestUnreadablePosition:
    def test_unparseable_slot_value_reports_position_unreadable(
        self, filter_wheel: IndiFilterWheelAdapter
    ) -> None:
        filter_wheel.connect()
        filter_wheel._client._vectors[(filter_wheel._device_name, "FILTER_SLOT")] = VectorState(  # noqa: SLF001
            state="Ok", elements={"FILTER_SLOT_VALUE": "not-a-number"}
        )
        status = filter_wheel.status()
        assert status.available is True
        assert status.current_slot is None
        assert status.reason == "position unreadable"


class TestMovingState:
    def test_busy_vector_state_reports_moving(self, filter_wheel: IndiFilterWheelAdapter) -> None:
        filter_wheel.connect()
        filter_wheel._client._vectors[(filter_wheel._device_name, "FILTER_SLOT")] = VectorState(  # noqa: SLF001
            state="Busy", elements={"FILTER_SLOT_VALUE": "4"}
        )
        status = filter_wheel.status()
        assert status.current_slot == 4
        assert status.moving is True


class TestPropertyRefresh:
    """Issue #34's own "external changes reflected" AC -- same technique
    as IndiFocuserAdapter's TestPropertyRefresh: poke the client's own
    cache directly (simulating a driver-pushed change with nothing of
    this app's own prompting it), then confirm status() re-observes the
    real (unpoked) server state once the throttle window passes."""

    def test_status_reflects_an_externally_driven_slot_change_after_refresh(
        self, filter_wheel: IndiFilterWheelAdapter
    ) -> None:
        filter_wheel.connect()
        assert filter_wheel.status().current_slot == 2

        filter_wheel._last_property_refresh = time.monotonic()  # noqa: SLF001
        filter_wheel._client._vectors[(filter_wheel._device_name, "FILTER_SLOT")] = VectorState(  # noqa: SLF001
            state="Ok", elements={"FILTER_SLOT_VALUE": "5"}
        )
        assert filter_wheel.status().current_slot == 5  # confirms the poke is what's cached

        time.sleep(0.25)
        assert filter_wheel.status().current_slot == 2  # re-announced by the real server
