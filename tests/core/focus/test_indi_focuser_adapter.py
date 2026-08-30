"""Full FocuserPort behavior for IndiFocuserAdapter against a real (loopback)
FakeIndiServer — unlike astrotool_core.mount's IndiMountAdapter, whose
connected paths are all `# pragma: no cover — requires a real OnStep
mount`, these connected paths get real coverage here."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import pytest
from astrotool_core.focus.indi_focuser_adapter import IndiFocuserAdapter
from astrotool_core.testing.fake_indi_server import FakeIndiServer


@pytest.fixture
def server() -> Iterator[FakeIndiServer]:
    fake = FakeIndiServer(start_position=5000, max_position=50000, move_delay_s=0.05)
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def focuser(server: FakeIndiServer) -> Iterator[IndiFocuserAdapter]:
    adapter = IndiFocuserAdapter(server.host, server.port, connect_timeout_s=2.0)
    yield adapter
    adapter.disconnect()


class TestNotConnected:
    def test_is_available_is_false(self) -> None:
        adapter = IndiFocuserAdapter("127.0.0.1", 1)
        assert adapter.is_available is False

    def test_status_reports_unavailable_and_zeroed(self) -> None:
        adapter = IndiFocuserAdapter("127.0.0.1", 1)
        status = adapter.status()
        assert status.available is False
        assert status.position == 0
        assert status.max_position == 0
        assert status.moving is False

    def test_move_is_a_safe_no_op(self) -> None:
        adapter = IndiFocuserAdapter("127.0.0.1", 1)
        adapter.move(5)  # must not raise

    def test_move_absolute_reports_not_accepted(self) -> None:
        adapter = IndiFocuserAdapter("127.0.0.1", 1)
        result = adapter.move_absolute(100)
        assert result.accepted is False
        assert result.target_position == 100

    def test_stop_is_a_safe_no_op(self) -> None:
        adapter = IndiFocuserAdapter("127.0.0.1", 1)
        adapter.stop()  # must not raise

    def test_connect_to_nothing_listening_raises_connection_error(self) -> None:
        adapter = IndiFocuserAdapter("127.0.0.1", 1, connect_timeout_s=0.5)
        with pytest.raises(ConnectionError):
            adapter.connect()


class TestVerboseDriverLogging:
    """connect() turns the driver's own DEBUG output fully on — see
    _enable_verbose_driver_logging's docstring (prompted by two focuser
    jitter reports where this app's own log showed nothing at all)."""

    def test_connect_enables_debug_and_all_debug_levels(
        self, focuser: IndiFocuserAdapter
    ) -> None:
        sent: list[tuple[str, dict[str, bool]]] = []
        original = focuser._client.send_new_switch_vector

        def spy(device: str, name: str, elements: dict[str, bool]) -> None:
            sent.append((name, elements))
            original(device, name, elements)

        focuser._client.send_new_switch_vector = spy  # type: ignore[method-assign]
        focuser.connect()

        sent_by_name = dict(sent)
        assert sent_by_name["DEBUG"] == {"ENABLE": True}
        assert sent_by_name["DEBUG_LEVEL"] == {
            "DBG_ERROR": True,
            "DBG_WARNING": True,
            "DBG_SESSION": True,
            "DBG_DEBUG": True,
            "DBG_EXTRA_1": True,
            "DBG_EXTRA_2": True,
        }

    def test_a_failure_enabling_verbose_logging_does_not_block_connect(
        self, focuser: IndiFocuserAdapter
    ) -> None:
        original = focuser._client.send_new_switch_vector

        def flaky(device: str, name: str, elements: dict[str, bool]) -> None:
            if name in ("DEBUG", "DEBUG_LEVEL"):
                raise ConnectionError("simulated: driver has no DEBUG property")
            original(device, name, elements)

        focuser._client.send_new_switch_vector = flaky  # type: ignore[method-assign]
        focuser.connect()  # must not raise despite the injected failure
        assert focuser.is_available is True


class TestConnected:
    def test_connect_makes_the_focuser_available(self, focuser: IndiFocuserAdapter) -> None:
        focuser.connect()
        assert focuser.is_available is True

    def test_initial_position_and_max_position(self, focuser: IndiFocuserAdapter) -> None:
        focuser.connect()
        assert focuser.get_position() == 5000
        assert focuser.get_max_position() == 50000

    def test_move_outward_increases_position(self, focuser: IndiFocuserAdapter) -> None:
        focuser.connect()
        focuser.move(10)
        _wait_for_position(focuser, 5010)

    def test_move_inward_decreases_position(self, focuser: IndiFocuserAdapter) -> None:
        focuser.connect()
        focuser.move(-10)
        _wait_for_position(focuser, 4990)

    def test_move_zero_steps_is_a_no_op(self, focuser: IndiFocuserAdapter) -> None:
        focuser.connect()
        focuser.move(0)
        assert focuser.get_position() == 5000

    def test_is_moving_true_while_busy(self) -> None:
        # A longer move_delay_s than the default fixture's, so there's a
        # reliably observable window between the move starting (Busy) and
        # completing (Ok) — the round trip to send move() and see its
        # first Busy update is itself not instantaneous, so a too-short
        # delay risks the move already finishing before the first check.
        fake = FakeIndiServer(start_position=5000, max_position=50000, move_delay_s=0.3)
        fake.start()
        try:
            focuser = IndiFocuserAdapter(fake.host, fake.port, connect_timeout_s=2.0)
            focuser.connect()
            try:
                focuser.move(10)
                _wait_until(lambda: focuser.is_moving() is True)
                _wait_until_idle(focuser)
                assert focuser.is_moving() is False
                assert focuser.get_position() == 5010
            finally:
                focuser.disconnect()
        finally:
            fake.stop()

    def test_move_absolute_is_accepted(self, focuser: IndiFocuserAdapter) -> None:
        focuser.connect()
        result = focuser.move_absolute(12345)
        assert result.accepted is True
        assert result.target_position == 12345
        assert result.start_position == 5000

    def test_status_reflects_connected_state(self, focuser: IndiFocuserAdapter) -> None:
        focuser.connect()
        status = focuser.status()
        assert status.available is True
        assert status.position == 5000
        assert status.max_position == 50000

    def test_stop_clears_the_busy_state(self) -> None:
        fake = FakeIndiServer(start_position=5000, max_position=50000, move_delay_s=0.3)
        fake.start()
        try:
            focuser = IndiFocuserAdapter(fake.host, fake.port, connect_timeout_s=2.0)
            focuser.connect()
            try:
                focuser.move(1000)
                _wait_until(lambda: focuser.is_moving() is True)
                focuser.stop()
                _wait_until_idle(focuser)
                assert focuser.is_moving() is False
            finally:
                focuser.disconnect()
        finally:
            fake.stop()

    def test_disconnect_makes_the_focuser_unavailable(self, focuser: IndiFocuserAdapter) -> None:
        focuser.connect()
        focuser.disconnect()
        assert focuser.is_available is False


class TestFocuserUnavailable:
    def test_connect_succeeds_but_focuser_is_not_available(self) -> None:
        fake = FakeIndiServer(focuser_available=False)
        fake.start()
        try:
            adapter = IndiFocuserAdapter(fake.host, fake.port, connect_timeout_s=2.0)
            adapter.connect()
            try:
                assert adapter.is_available is False
                assert adapter.get_position() == 0
                assert adapter.get_max_position() == 0
                result = adapter.move_absolute(100)
                assert result.accepted is False
            finally:
                adapter.disconnect()
        finally:
            fake.stop()


def _wait_until_idle(focuser: IndiFocuserAdapter, timeout_s: float = 2.0) -> None:
    _wait_until(
        lambda: not focuser.is_moving(), timeout_s=timeout_s, message="focuser never went idle"
    )


def _wait_for_position(focuser: IndiFocuserAdapter, expected: int, timeout_s: float = 2.0) -> None:
    _wait_until(
        lambda: focuser.get_position() == expected,
        timeout_s=timeout_s,
        message=f"focuser never reached position {expected} (stuck at {focuser.get_position()})",
    )


def _wait_until(
    predicate: Callable[[], bool], timeout_s: float = 2.0, message: str = "condition never met"
) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.01)
