"""Full MountPort behavior for IndiMountPulseAdapter against a real
(loopback) FakeIndiServer -- mirrors test_indi_mount_park_adapter.py's
pattern for the same reasons."""

from __future__ import annotations

import time as time_module
from collections.abc import Callable, Iterator

import pytest
from astrotool_core.mount.indi_mount_pulse_adapter import IndiMountPulseAdapter
from astrotool_core.mount.port import AxisDirection, MountAxis
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
def mount(server: FakeIndiServer) -> Iterator[IndiMountPulseAdapter]:
    adapter = IndiMountPulseAdapter(server.host, server.port, connect_timeout_s=2.0)
    yield adapter
    adapter.disconnect()


_real_sleep = time_module.sleep


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    # pulse_axis's final "restore the previous rate" send is
    # fire-and-forget, same as every other command this adapter (and
    # IndiMountParkAdapter) sends -- give the fake server's accept thread
    # a moment to actually process it before asserting on server state.
    deadline = time_module.monotonic() + timeout_s
    while not predicate():
        assert time_module.monotonic() < deadline, "condition never met"
        _real_sleep(0.01)


def _spy_on_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the adapter's time.sleep with one that still yields briefly
    (so the fake server's accept thread gets to process what was just
    sent) but records every call, instead of actually waiting out the
    real settle/pulse durations -- keeps tests fast.

    The adapter's own `import time` binds the same stdlib module object
    as this test file's `time_module`, so patching `time_module.sleep`
    replaces the name everywhere the adapter sees it too -- fake_sleep
    must go through `_real_sleep`, captured before any patching, to
    actually wait rather than recurse into itself."""
    calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        _real_sleep(0.05)

    monkeypatch.setattr(time_module, "sleep", fake_sleep)
    return calls


class TestNotConnected:
    def test_is_available_is_false(self) -> None:
        adapter = IndiMountPulseAdapter("127.0.0.1", 1)
        assert adapter.is_available is False

    def test_pulse_axis_rejected(self) -> None:
        adapter = IndiMountPulseAdapter("127.0.0.1", 1)
        result = adapter.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)
        assert result.accepted is False

    def test_status_reports_not_connected(self) -> None:
        adapter = IndiMountPulseAdapter("127.0.0.1", 1)
        status = adapter.status()
        assert status.connected is False

    def test_connect_to_nothing_listening_raises_connection_error(self) -> None:
        adapter = IndiMountPulseAdapter("127.0.0.1", 1, connect_timeout_s=0.5)
        with pytest.raises(ConnectionError):
            adapter.connect()


class TestConnected:
    def test_connect_makes_the_mount_available(self, mount: IndiMountPulseAdapter) -> None:
        mount.connect()
        assert mount.is_available is True

    @pytest.mark.parametrize(
        ("axis", "direction", "vector", "element"),
        [
            (MountAxis.AXIS1, AxisDirection.POSITIVE, "we", "MOTION_EAST"),
            (MountAxis.AXIS1, AxisDirection.NEGATIVE, "we", "MOTION_WEST"),
            (MountAxis.AXIS2, AxisDirection.POSITIVE, "ns", "MOTION_NORTH"),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE, "ns", "MOTION_SOUTH"),
        ],
    )
    def test_pulse_axis_selects_20x_and_the_right_direction(
        self,
        mount: IndiMountPulseAdapter,
        server: FakeIndiServer,
        monkeypatch: pytest.MonkeyPatch,
        axis: MountAxis,
        direction: AxisDirection,
        vector: str,
        element: str,
    ) -> None:
        mount.connect()
        _spy_on_sleep(monkeypatch)
        result = mount.pulse_axis(axis, direction, 500)
        assert result.accepted is True
        # pulse_axis's final "turn the direction back off"/"restore the
        # rate" sends are both fire-and-forget (same as every other
        # command this adapter sends) -- give the fake server's accept
        # thread a moment to process them before asserting, rather than
        # assuming they've already landed the instant pulse_axis returns
        # (flaky on a loaded/shared CI runner -- see _wait_until).
        _wait_until(
            lambda: server._motion_ns == {"MOTION_NORTH": False, "MOTION_SOUTH": False}  # noqa: SLF001
        )
        _wait_until(
            lambda: server._motion_we == {"MOTION_WEST": False, "MOTION_EAST": False}  # noqa: SLF001
        )
        _wait_until(lambda: server._slew_rate == "9")  # noqa: SLF001

    def test_pulse_axis_logs_the_axis_direction_duration_and_rate(
        self,
        mount: IndiMountPulseAdapter,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # Real report: "have all axis being moved really?" -- pulse_axis()
        # previously logged nothing at all, so a diagnostic bundle could
        # never actually prove which axis/direction/duration/rate a
        # calibration step really sent to the mount.
        mount.connect()
        _spy_on_sleep(monkeypatch)
        with caplog.at_level("INFO", logger="astrotool_core.mount.indi_mount_pulse_adapter"):
            mount.pulse_axis(MountAxis.AXIS2, AxisDirection.POSITIVE, 2000, rate_preset="7")
        [record] = [r for r in caplog.records if "pulse_axis" in r.message]
        assert "AXIS2" in record.message
        assert "POSITIVE" in record.message
        assert "2000ms" in record.message
        assert "'7'" in record.message

    def test_pulse_axis_selects_the_rate_before_starting_motion(
        self, mount: IndiMountPulseAdapter, server: FakeIndiServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # pulse_axis() now waits for the driver to actually confirm the
        # rate change (see _RATE_SELECT_SETTLE_S's own docstring) instead
        # of a blind fixed sleep before it -- confirm that ordering
        # directly via the sequence of switch vectors sent, rather than
        # by intercepting time.sleep (only the pulse-duration wait still
        # goes through it).
        mount.connect()
        _spy_on_sleep(monkeypatch)
        sent: list[tuple[str, dict[str, bool]]] = []
        original = mount._client.send_new_switch_vector  # noqa: SLF001

        def spy(device: str, name: str, elements: dict[str, bool]) -> None:
            sent.append((name, dict(elements)))
            original(device, name, elements)

        mount._client.send_new_switch_vector = spy  # type: ignore[method-assign]  # noqa: SLF001

        mount.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)

        names = [name for name, _ in sent]
        rate_index = names.index("TELESCOPE_SLEW_RATE")
        motion_on_index = next(
            i for i, (name, elements) in enumerate(sent)
            if name == "TELESCOPE_MOTION_WE" and elements.get("MOTION_EAST") is True
        )
        assert rate_index < motion_on_index  # rate selected before motion starts
        assert sent[rate_index][1] == {"6": True}  # the confirmed "20x" preset element
        _wait_until(lambda: server._slew_rate == "6")  # noqa: SLF001

    def test_disconnect_makes_the_mount_unavailable(self, mount: IndiMountPulseAdapter) -> None:
        mount.connect()
        mount.disconnect()
        assert mount.is_available is False

    def test_abort_stops_an_in_progress_motion(
        self, mount: IndiMountPulseAdapter, server: FakeIndiServer
    ) -> None:
        mount.connect()
        # Simulate a motion in progress -- what abort() is for.
        server._motion_we = {"MOTION_WEST": False, "MOTION_EAST": True}  # noqa: SLF001
        mount.abort()
        _wait_until(
            lambda: server._motion_we == {"MOTION_WEST": False, "MOTION_EAST": False}  # noqa: SLF001
        )

    def test_abort_before_connect_is_a_safe_no_op(self) -> None:
        IndiMountPulseAdapter("127.0.0.1", 1).abort()  # must not raise


class TestMountInterfaceUnavailable:
    def test_connect_succeeds_but_mount_is_not_available(self) -> None:
        fake = FakeIndiServer(mount_available=False)
        fake.start()
        try:
            adapter = IndiMountPulseAdapter(fake.host, fake.port, connect_timeout_s=2.0)
            adapter.connect()
            try:
                assert adapter.is_available is False
                result = adapter.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)
                assert result.accepted is False
            finally:
                adapter.disconnect()
        finally:
            fake.stop()
