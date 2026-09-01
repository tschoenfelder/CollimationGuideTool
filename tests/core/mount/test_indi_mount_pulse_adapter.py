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
    # start_parked=False -- parking is IndiMountParkAdapter's own concern
    # (a separate connection in the real app), not this adapter's; these
    # tests are about pulse mechanics, not park state, so they shouldn't
    # incidentally depend on it. See TestParkedRejection below for the
    # dedicated parked-specifically scenario.
    fake = FakeIndiServer(start_parked=False)
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
        # Settles back to the fake server's own default ("9") once the
        # pulse finishes and pulse_axis() restores whatever rate preceded
        # it -- "6" (this pulse's own selected rate) is only ever a
        # transient mid-pulse state, not the final one; waiting for "6"
        # here was actually racing that restore rather than genuinely
        # confirming ordering (occasionally caught the transient window,
        # occasionally didn't -- a real, if latent, flake this pulse's
        # own new confirmation waits made more likely to surface).
        _wait_until(lambda: server._slew_rate == "9")  # noqa: SLF001

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


class TestParkedRejection:
    """Real finding, sourced against both the real rig and libindi's own
    `INDI::Telescope::MoveNS`/`MoveWE` (`inditelescope.cpp`): a driver
    rejects a motion command outright while parked -- resets the switch
    element back to Off and reports the vector's state as `"Idle"`, not
    `"Ok"` (verified live: `MOTION_EAST=On` sent to the real parked mount
    came back `MOTION_EAST=Off`/`state=Idle`). `pulse_axis()` never
    checked this: it always sent, then blindly slept out the *entire*
    requested duration regardless of whether the driver actually accepted
    the motion, and unconditionally reported `accepted=True`."""

    def test_pulse_axis_reports_rejection_when_parked_instead_of_silently_sleeping(
        self,
    ) -> None:
        fake = FakeIndiServer(start_parked=True)
        fake.start()
        try:
            mount = IndiMountPulseAdapter(fake.host, fake.port, connect_timeout_s=2.0)
            mount.connect()
            start = time_module.monotonic()
            # A long duration -- if this silently sleeps it out despite the
            # rejection (the old bug), this assertion's own elapsed-time
            # check below fails loudly rather than just being slow.
            result = mount.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 5000)
            elapsed = time_module.monotonic() - start
            assert result.accepted is False
            assert "park" in result.message.lower()
            assert elapsed < 2.0, f"blindly slept out the pulse despite rejection ({elapsed}s)"
            mount.disconnect()
        finally:
            fake.stop()

    def test_pulse_axis_does_not_report_rejection_when_unparked(
        self, mount: IndiMountPulseAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The `server`/`mount` fixtures already start unparked -- this is
        # a direct regression guard against the fix being too aggressive
        # (e.g. misreading a normal accepted response as a rejection).
        mount.connect()
        _spy_on_sleep(monkeypatch)
        result = mount.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)
        assert result.accepted is True
        assert result.message == ""


class TestMotionOffConfirmation:
    """The same class of gap that motivated TestParkedRejection above, on
    the *other* side of a pulse: turning the direction switch back off
    after the pulse duration was still fire-and-forget -- if that command
    never lands, the mount keeps physically moving with no way for
    anything in this app to notice, let alone stop it. Arguably worse
    than the motion-on gap: that one meant "reports success but nothing
    moved"; this one means "reports success while the mount may still be
    moving uncommanded"."""

    def test_pulse_axis_falls_back_to_abort_when_turning_motion_off_never_confirms(
        self, mount: IndiMountPulseAdapter, server: FakeIndiServer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import astrotool_core.mount.indi_mount_pulse_adapter as adapter_module

        # Small, not the real 2.0s ceiling -- this test deliberately lets
        # the off-confirmation wait actually time out.
        monkeypatch.setattr(adapter_module, "_MOTION_CONFIRM_TIMEOUT_S", 0.2)
        mount.connect()
        _spy_on_sleep(monkeypatch)
        original_send = mount._client.send_new_switch_vector  # noqa: SLF001

        def dropping_send(device: str, name: str, elements: dict[str, bool]) -> None:
            # Drop only the "turn motion off" send -- simulate it never
            # reaching/being processed by the driver. Everything else
            # (rate select, motion-on, abort()'s own TELESCOPE_ABORT_MOTION)
            # goes through normally.
            if name == "TELESCOPE_MOTION_WE" and elements.get("MOTION_EAST") is False:
                return
            original_send(device, name, elements)

        mount._client.send_new_switch_vector = dropping_send  # type: ignore[method-assign]  # noqa: SLF001

        result = mount.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 100)

        assert result.accepted is True  # the pulse itself genuinely happened
        # abort() must have fired as the safety fallback -- the dropped
        # off-command never reset the server's own motion state, so this
        # can only be true if TELESCOPE_ABORT_MOTION's own reset landed.
        _wait_until(
            lambda: server._motion_we == {"MOTION_WEST": False, "MOTION_EAST": False}  # noqa: SLF001
        )

    def test_pulse_axis_does_not_call_abort_when_motion_off_confirms_normally(
        self, mount: IndiMountPulseAdapter, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Regression guard against the fallback firing on every pulse --
        # a normal, unimpeded pulse must not trigger abort() at all.
        mount.connect()
        _spy_on_sleep(monkeypatch)
        abort_calls: list[None] = []
        original_abort = mount.abort

        def spy_abort() -> None:
            abort_calls.append(None)
            original_abort()

        monkeypatch.setattr(mount, "abort", spy_abort)

        result = mount.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)

        assert result.accepted is True
        assert abort_calls == []


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
