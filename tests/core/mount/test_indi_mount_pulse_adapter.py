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
        # By the time pulse_axis returns, the pulse is over -- direction
        # switches back off and the original ("9"/Max) rate restored.
        assert server._motion_ns == {"MOTION_NORTH": False, "MOTION_SOUTH": False}  # noqa: SLF001
        assert server._motion_we == {"MOTION_WEST": False, "MOTION_EAST": False}  # noqa: SLF001
        _wait_until(lambda: server._slew_rate == "9")  # noqa: SLF001

    def test_pulse_axis_selects_20x_preset_mid_pulse(
        self, mount: IndiMountPulseAdapter, server: FakeIndiServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mount.connect()
        # Capture server state at each of pulse_axis's two internal
        # sleeps: rate-select settle, then the pulse duration itself.
        snapshots: list[tuple[str, dict[str, bool]]] = []

        def fake_sleep(seconds: float) -> None:
            _real_sleep(0.05)
            snapshots.append((server._slew_rate, dict(server._motion_we)))  # noqa: SLF001

        monkeypatch.setattr(time_module, "sleep", fake_sleep)
        mount.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)
        assert len(snapshots) == 2
        rate_after_select, motion_after_select = snapshots[0]
        rate_during_pulse, motion_during_pulse = snapshots[1]
        assert rate_after_select == "6"  # the confirmed "20x" preset element
        assert motion_after_select == {"MOTION_WEST": False, "MOTION_EAST": False}
        assert rate_during_pulse == "6"
        assert motion_during_pulse == {"MOTION_WEST": False, "MOTION_EAST": True}

    def test_disconnect_makes_the_mount_unavailable(self, mount: IndiMountPulseAdapter) -> None:
        mount.connect()
        mount.disconnect()
        assert mount.is_available is False


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
