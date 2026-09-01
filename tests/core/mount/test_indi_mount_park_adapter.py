"""Full MountParkPort behavior for IndiMountParkAdapter against a real
(loopback) FakeIndiServer — mirrors test_indi_focuser_adapter.py's
pattern for the same reasons (real coverage of the connected paths,
unlike IndiMountAdapter's hardware-only ones)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import pytest
from astrotool_core.mount.indi_mount_park_adapter import IndiMountParkAdapter
from astrotool_core.testing.fake_indi_server import FakeIndiServer


@pytest.fixture
def server() -> Iterator[FakeIndiServer]:
    fake = FakeIndiServer(start_parked=True, park_delay_s=0.05)
    fake.start()
    try:
        yield fake
    finally:
        fake.stop()


@pytest.fixture
def mount(server: FakeIndiServer) -> Iterator[IndiMountParkAdapter]:
    adapter = IndiMountParkAdapter(server.host, server.port, connect_timeout_s=2.0)
    yield adapter
    adapter.disconnect()


class TestNotConnected:
    def test_is_available_is_false(self) -> None:
        adapter = IndiMountParkAdapter("127.0.0.1", 1)
        assert adapter.is_available is False

    def test_status_reports_unavailable_and_not_parked_or_tracking(self) -> None:
        adapter = IndiMountParkAdapter("127.0.0.1", 1)
        status = adapter.status()
        assert status.available is False
        assert status.parked is False
        assert status.tracking is False

    def test_park_is_a_safe_no_op(self) -> None:
        adapter = IndiMountParkAdapter("127.0.0.1", 1)
        adapter.park()  # must not raise

    def test_unpark_is_a_safe_no_op(self) -> None:
        adapter = IndiMountParkAdapter("127.0.0.1", 1)
        adapter.unpark()  # must not raise

    def test_connect_to_nothing_listening_raises_connection_error(self) -> None:
        adapter = IndiMountParkAdapter("127.0.0.1", 1, connect_timeout_s=0.5)
        with pytest.raises(ConnectionError):
            adapter.connect()


class TestConnected:
    def test_connect_makes_the_mount_available(self, mount: IndiMountParkAdapter) -> None:
        mount.connect()
        assert mount.is_available is True

    def test_starts_parked_with_tracking_off(self, mount: IndiMountParkAdapter) -> None:
        mount.connect()
        status = mount.status()
        assert status.parked is True
        assert status.tracking is False

    def test_unpark_clears_parked_state(self, mount: IndiMountParkAdapter) -> None:
        mount.connect()
        mount.unpark()
        _wait_until(lambda: not mount.status().parked)
        assert mount.status().parked is False

    def test_unpark_directly_deactivates_tracking(self, server: FakeIndiServer) -> None:
        # Simulate the mount having been left tracking (e.g. a prior
        # session) -- unpark() must turn it off regardless, per
        # MountParkPort.unpark()'s contract, not just leave whatever
        # tracking state the mount happened to already report.
        mount = IndiMountParkAdapter(server.host, server.port, connect_timeout_s=2.0)
        mount.connect()
        server._tracking = True  # noqa: SLF001 -- test setup, simulating prior state
        mount.unpark()
        _wait_until(lambda: not mount.status().tracking)
        assert mount.status().tracking is False
        mount.disconnect()

    def test_park_sets_parked_state(self, mount: IndiMountParkAdapter) -> None:
        mount.connect()
        mount.unpark()
        _wait_until(lambda: not mount.status().parked)
        mount.park()
        _wait_until(lambda: mount.status().parked)
        assert mount.status().parked is True

    def test_disconnect_makes_the_mount_unavailable(self, mount: IndiMountParkAdapter) -> None:
        mount.connect()
        mount.disconnect()
        assert mount.is_available is False

    def test_stop_tracking_deactivates_tracking_without_parking(
        self, server: FakeIndiServer
    ) -> None:
        # Real report: the mount kept tracking after quitting the app.
        mount = IndiMountParkAdapter(server.host, server.port, connect_timeout_s=2.0)
        mount.connect()
        mount.unpark()
        _wait_until(lambda: not mount.status().parked)
        server._tracking = True  # noqa: SLF001 -- simulate tracking left on
        mount.stop_tracking()
        _wait_until(lambda: not mount.status().tracking)
        assert mount.status().tracking is False
        assert mount.status().parked is False  # deliberately not parked
        mount.disconnect()

    def test_stop_tracking_on_a_disconnected_mount_is_a_safe_no_op(self) -> None:
        adapter = IndiMountParkAdapter("127.0.0.1", 1)
        adapter.stop_tracking()  # must not raise


class TestUnparkOvercomesDriverTrackOnOverride:
    """Regression test for incident 25446102 ("Shows unparked, tracking,
    but don't stop tracking") -- a real-hardware trace caught OnStep's
    driver pushing its own delayed TRACK_ON shortly after UNPARK,
    overriding the adapter's own TRACK_OFF. `unpark()`'s retries (see its
    docstring) must win the race once the driver's override has landed."""

    def test_tracking_ends_up_off_despite_a_delayed_driver_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import astrotool_core.mount.indi_mount_park_adapter as adapter_module

        # Real delays (seconds, not fractions) would make this test slow
        # for no benefit -- only the "does a retry land after the
        # override" behavior is under test here.
        monkeypatch.setattr(adapter_module, "_TRACK_OFF_RETRY_DELAYS_S", (0.15,))
        fake = FakeIndiServer(
            start_parked=True, park_delay_s=0.05, auto_track_on_after_unpark_delay_s=0.05
        )
        fake.start()
        try:
            mount = IndiMountParkAdapter(fake.host, fake.port, connect_timeout_s=2.0)
            mount.connect()
            mount.unpark()
            # The driver's own override should land first, same as on
            # the real rig -- confirming the test double actually
            # reproduces the race before checking the fix overcomes it.
            _wait_until(
                lambda: mount.status().tracking, message="driver override never observed"
            )
            _wait_until(lambda: not mount.status().tracking, message="retry never landed")
            assert mount.status().tracking is False
            mount.disconnect()
        finally:
            fake.stop()

    def test_a_quick_repark_does_not_cancel_the_retry_that_still_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Real-hardware regression: the "test move" feature unparks,
        pulses (~1s), and re-parks -- all comfortably within the driver's
        own ~1.5s delayed auto-track-on window. park() must not cancel
        unpark()'s pending retries just because the mount is parked again
        by the time the driver's override lands -- it can still land
        *after* park(), and TRACK_OFF is idempotent regardless of park
        state (see park()'s own docstring)."""
        import astrotool_core.mount.indi_mount_park_adapter as adapter_module

        monkeypatch.setattr(adapter_module, "_TRACK_OFF_RETRY_DELAYS_S", (0.2,))
        fake = FakeIndiServer(
            start_parked=True, park_delay_s=0.02, auto_track_on_after_unpark_delay_s=0.1
        )
        fake.start()
        try:
            mount = IndiMountParkAdapter(fake.host, fake.port, connect_timeout_s=2.0)
            mount.connect()
            mount.unpark()
            _wait_until(lambda: not mount.status().parked)
            mount.park()  # re-parks well before the 0.2s retry fires
            _wait_until(lambda: mount.status().parked)
            # Confirm the driver's override actually lands *after*
            # park() first -- without this, "tracking is already False"
            # would trivially pass regardless of whether the retry ever
            # does anything (the mistake an earlier version of this test
            # made: checking "not tracking" before the override had even
            # had a chance to fire proves nothing).
            _wait_until(
                lambda: mount.status().tracking,
                message="driver override never observed after re-park",
            )
            # ...but the retry (at 0.2s) must still win afterward -- not
            # stuck on because park() cancelled it.
            _wait_until(
                lambda: not mount.status().tracking, message="retry never landed after re-park"
            )
            assert mount.status().tracking is False
            mount.disconnect()
        finally:
            fake.stop()


class TestPropertyRefresh:
    """Regression coverage for the real report: "Calling the APP still
    shows ... mount as being unparked, even so I left it parked. The
    state should always been taken from indiserver." status() must not
    trust a client-side cache forever once thrown off -- see
    _PROPERTY_REFRESH_INTERVAL_S's own docstring for the reasoning."""

    def test_status_self_corrects_a_stale_cached_value_after_the_refresh_interval(
        self, mount: IndiMountParkAdapter
    ) -> None:
        from astrotool_core.indi.client import VectorState

        mount.connect()
        assert mount.status().parked is True
        # Pin the throttle window deterministically (rather than relying
        # on real elapsed time not crossing it by accident) right after a
        # real refresh, then poke the cache stale *without* going through
        # the server -- simulating a driver's post-connect report having
        # been wrong with nothing of its own prompting a correction.
        mount._last_property_refresh = time.monotonic()  # noqa: SLF001
        mount._client._vectors[(mount._device_name, "TELESCOPE_PARK")] = VectorState(  # noqa: SLF001
            state="Ok", elements={"PARK": "Off", "UNPARK": "On"}
        )
        assert mount.status().parked is False  # confirms the poke really is what's cached

        time.sleep(0.25)
        assert mount.status().parked is True  # re-announced by the real (still-parked) server

    def test_status_does_not_refresh_faster_than_the_throttle_interval(
        self, mount: IndiMountParkAdapter
    ) -> None:
        mount.connect()
        mount.status()  # the very first status() call always refreshes once (see __init__)
        sent: list[str | None] = []
        original = mount._client.send_get_properties

        def spy(device: str | None = None) -> None:
            sent.append(device)
            original(device)

        mount._client.send_get_properties = spy  # type: ignore[method-assign]
        mount.status()
        mount.status()
        assert sent == []  # well within the real (multi-second) default throttle window


class TestMountInterfaceUnavailable:
    def test_connect_succeeds_but_mount_is_not_available(self) -> None:
        fake = FakeIndiServer(mount_available=False)
        fake.start()
        try:
            adapter = IndiMountParkAdapter(fake.host, fake.port, connect_timeout_s=2.0)
            adapter.connect()
            try:
                assert adapter.is_available is False
                status = adapter.status()
                assert status.available is False
                assert status.parked is False
                assert status.tracking is False
                adapter.park()  # must not raise
                adapter.unpark()  # must not raise
            finally:
                adapter.disconnect()
        finally:
            fake.stop()


def _wait_until(
    predicate: Callable[[], bool], timeout_s: float = 2.0, message: str = "condition never met"
) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.01)
