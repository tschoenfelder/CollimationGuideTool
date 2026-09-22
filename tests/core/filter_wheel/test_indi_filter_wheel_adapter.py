"""Full FilterWheelPort behavior for IndiFilterWheelAdapter against a
real (loopback) FakeIndiServer -- issue #34 (read-only status) + issue #47
(commanding a slot change)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

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


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestSetSlot:
    def test_commands_the_device_and_reports_moving_then_arrived(
        self, filter_wheel: IndiFilterWheelAdapter
    ) -> None:
        filter_wheel.connect()
        assert filter_wheel.status().current_slot == 2

        filter_wheel.set_slot(4)
        # The Busy push is a real round trip over the fake server's socket --
        # give the client's reader thread a moment to receive/parse it.
        assert _wait_until(lambda: filter_wheel.status().moving is True)

        assert _wait_until(lambda: filter_wheel.status().moving is False)
        status = filter_wheel.status()
        assert status.moving is False
        assert status.current_slot == 4

    def test_a_second_move_while_the_first_is_in_progress_is_refused(
        self, filter_wheel: IndiFilterWheelAdapter
    ) -> None:
        filter_wheel.connect()
        filter_wheel.set_slot(3)
        assert _wait_until(lambda: filter_wheel.status().moving is True)

        with pytest.raises(RuntimeError, match="already in progress"):
            filter_wheel.set_slot(4)

    def test_not_connected_is_a_safe_no_op(self) -> None:
        adapter = IndiFilterWheelAdapter("127.0.0.1", 1, _DEVICE_NAME)
        adapter.set_slot(1)  # must not raise

    def test_connected_but_no_wheel_detected_is_a_safe_no_op(self) -> None:
        fake = FakeIndiServer(device_name=_DEVICE_NAME, filter_wheel_available=False)
        fake.start()
        adapter = IndiFilterWheelAdapter(fake.host, fake.port, _DEVICE_NAME, connect_timeout_s=2.0)
        try:
            adapter.connect()
            adapter.set_slot(1)  # must not raise
        finally:
            adapter.disconnect()
            fake.stop()


class TestFilterNamePrecedence:
    """Issue #47: device-reported name wins; the configured fallback only
    fills in a slot the device itself doesn't name."""

    def test_device_reported_name_wins_over_the_configured_fallback(self) -> None:
        fake = FakeIndiServer(
            device_name=_DEVICE_NAME, filter_slot=2, filter_names=("Luminance", "Red")
        )
        fake.start()
        adapter = IndiFilterWheelAdapter(
            fake.host, fake.port, _DEVICE_NAME, connect_timeout_s=2.0, filter_names={2: "X"}
        )
        try:
            adapter.connect()
            assert adapter.status().filter_name == "Red"  # device wins, not the configured "X"
        finally:
            adapter.disconnect()
            fake.stop()

    def test_configured_fallback_is_used_when_the_device_names_nothing(self) -> None:
        fake = FakeIndiServer(device_name=_DEVICE_NAME, filter_slot=3, filter_names=None)
        fake.start()
        adapter = IndiFilterWheelAdapter(
            fake.host, fake.port, _DEVICE_NAME, connect_timeout_s=2.0, filter_names={3: "G"}
        )
        try:
            adapter.connect()
            assert adapter.status().filter_name == "G"
        finally:
            adapter.disconnect()
            fake.stop()

    def test_neither_source_leaves_the_name_none(self) -> None:
        fake = FakeIndiServer(device_name=_DEVICE_NAME, filter_slot=6, filter_names=None)
        fake.start()
        adapter = IndiFilterWheelAdapter(
            fake.host, fake.port, _DEVICE_NAME, connect_timeout_s=2.0, filter_names={3: "G"}
        )
        try:
            adapter.connect()
            assert adapter.status().filter_name is None
        finally:
            adapter.disconnect()
            fake.stop()


class TestSlotNames:
    def test_reports_a_name_per_slot_device_first_then_configured(self) -> None:
        fake = FakeIndiServer(
            device_name=_DEVICE_NAME, filter_slot=1, filter_names=("Luminance", "", "Green")
        )
        fake.start()
        adapter = IndiFilterWheelAdapter(
            fake.host, fake.port, _DEVICE_NAME, connect_timeout_s=2.0, filter_names={2: "R"}
        )
        try:
            adapter.connect()
            assert adapter.slot_names() == {1: "Luminance", 2: "R", 3: "Green"}
        finally:
            adapter.disconnect()
            fake.stop()

    def test_before_connect_returns_only_the_configured_names(self) -> None:
        adapter = IndiFilterWheelAdapter(
            "127.0.0.1", 1, _DEVICE_NAME, filter_names={1: "L", 2: "R"}
        )
        assert adapter.slot_names() == {1: "L", 2: "R"}


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
