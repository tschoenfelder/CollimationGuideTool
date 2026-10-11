"""S6.3 (#52 L01/L04): the one-action-at-a-time confirmation of FocuserPanel,
FilterWheelPanel and MountParkPanel, and their periodic status polls.

- L01: MountParkPanel still had the pre-3fc09ae shape -- its safety-net timeout sat in an
  `elif` reachable only while the mount was NOT "busy", so a park/unpark that never reached
  its target left Park/Unpark disabled forever (ports without `long_running_actions`).
- L01 (FocuserPanel): after the safety-net timeout the in-flight window closes, but In/Out
  stay disabled while the driver itself still reports Busy (documented `_on_stop`
  behaviour); Stop stays the escape hatch.
- L04: a status read that raises after connect (an INDI drop, an OnStepAdapter error) used
  to propagate out of the QTimer slot into Qt on every tick; the poll now shows the reason,
  logs it once per failure streak, keeps the safety-net timeout running and recovers.
- S4 (b): an out-of-range filter slot answered with Alert ends the slot change at once
  with the driver's rejection shown, instead of after the 10 s timeout.

Every poll is one `_poll_status()` call on a `FakeClock`; no Qt timer, no real waiting.
"""

from __future__ import annotations

import logging

import pytest
from astrotool_core.filter_wheel.fake_filter_wheel import FakeFilterWheel
from astrotool_core.filter_wheel.port import FilterWheelState
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.port import FocuserStatus
from astrotool_core.mount.park_port import MountParkStatus
from astrotool_core.testing import FilterWheelScenario, make_simulated_filter_wheel_adapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.timing import FakeClock
from collimation_tool.ui.filter_wheel_panel import FilterWheelPanel
from collimation_tool.ui.focuser_panel import FocuserPanel
from collimation_tool.ui.mount_park_panel import MountParkPanel


class _BreakableFocuser(FakeFocuser):
    """FakeFocuser whose status reads raise while `broken`, and whose Busy is scripted."""

    def __init__(self) -> None:
        super().__init__()
        self.broken = False
        self.moving = False

    def status(self) -> FocuserStatus:
        if self.broken:
            raise RuntimeError("INDI focuser status unreadable")
        return FocuserStatus(True, self.get_position(), 10_000, self.moving)

    @property
    def is_available(self) -> bool:
        return self.status().available

    def is_moving(self) -> bool:
        return self.status().moving


class _BreakableWheel(FakeFilterWheel):
    def __init__(self) -> None:
        super().__init__(slot=1, filter_names={1: "L", 2: "R"})
        self.broken = False

    def status(self) -> FilterWheelState:
        if self.broken:
            raise ConnectionError("IndiClient: send failed, connection lost")
        return super().status()

    def slot_names(self) -> dict[int, str]:
        if self.broken:
            raise ConnectionError("IndiClient: send failed, connection lost")
        return super().slot_names()


class _BreakablePark(FakeMountPark):
    """Park port whose reads raise while `broken`; `stuck` = park/unpark never take effect."""

    def __init__(self, *, start_parked: bool) -> None:
        super().__init__(start_parked=start_parked)
        self.broken = False
        self.stuck = False

    def status(self) -> MountParkStatus:
        if self.broken:
            raise KeyError("UTC")
        return super().status()

    @property
    def is_available(self) -> bool:
        return self.status().available

    def park(self) -> None:
        if not self.stuck:
            super().park()

    def unpark(self) -> None:
        if not self.stuck:
            super().unpark()


def _warnings(caplog: pytest.LogCaptureFixture, text: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.WARNING and text in r.getMessage()]


class TestMountParkPanelStuckTransition:
    """L01: the pre-3fc09ae `elif` ordering, still live in MountParkPanel."""

    def test_a_park_that_never_takes_effect_is_released_by_the_safety_net(
        self, qapp: object
    ) -> None:
        clock = FakeClock()
        mount = _BreakablePark(start_parked=False)
        panel = MountParkPanel(mount, clock=clock)
        panel._connect_button.setChecked(True)
        mount.stuck = True
        panel._on_park()
        panel._poll_status()  # still unparked: "not yet settled" = busy, seen
        assert panel._action_in_flight
        clock.advance(30.0)
        panel._poll_status()
        assert panel._action_in_flight  # exactly at the timeout: still awaited
        clock.advance(0.25)
        panel._poll_status()
        assert not panel._action_in_flight
        assert panel._park_button.isEnabled()  # retryable
        panel.stop()


class TestFocuserPanelStuckBusy:
    """L01 (FocuserPanel): code and comment now say the same thing."""

    def test_after_the_timeout_in_out_follow_the_drivers_busy_and_stop_stays_enabled(
        self, qapp: object
    ) -> None:
        clock = FakeClock()
        focuser = _BreakableFocuser()
        panel = FocuserPanel(focuser, clock=clock)
        released: list[bool] = []
        panel.move_in_flight_changed.connect(released.append)
        panel._connect_button.setChecked(True)
        panel._on_move_out()
        focuser.moving = True  # the driver reports Busy -- and never clears it
        panel._poll_status()
        clock.advance(10.25)
        panel._poll_status()
        assert panel._move_in_flight is False and released[-1] is False
        assert not panel._in_button.isEnabled() and not panel._out_button.isEnabled()
        assert panel._stop_button.isEnabled()
        focuser.moving = False
        panel._poll_status()
        assert panel._in_button.isEnabled() and panel._out_button.isEnabled()
        panel.stop()


class TestStatusPollsNeverRaise:
    """L04: a raising status read is shown, logged once per streak, and survived."""

    def test_focuser_panel(self, qapp: object, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        focuser = _BreakableFocuser()
        panel = FocuserPanel(focuser, title="Main focuser", clock=clock)
        panel._connect_button.setChecked(True)
        panel._on_move_out()
        focuser.broken = True
        with caplog.at_level(logging.WARNING):
            panel._poll_status()
            panel._poll_status()
        assert "INDI focuser status unreadable" in panel._status_label.text()
        assert len(_warnings(caplog, "INDI focuser status unreadable")) == 1
        assert not panel._in_button.isEnabled()
        assert panel._stop_button.isEnabled()  # the escape hatch stays
        clock.advance(10.25)
        panel._poll_status()  # the safety net still runs while reads fail
        assert panel._move_in_flight is False
        focuser.broken = False
        panel._poll_status()
        assert panel._status_label.text().startswith("Position")
        assert panel._in_button.isEnabled()
        panel.stop()

    def test_filter_wheel_panel(self, qapp: object, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        wheel = _BreakableWheel()
        panel = FilterWheelPanel(wheel, selectable=True, clock=clock)
        panel._connect_button.setChecked(True)
        panel._poll_status()
        panel._slot_combo.setCurrentIndex(1)
        panel._on_set_clicked()
        wheel.broken = True
        with caplog.at_level(logging.WARNING):
            panel._poll_status()
            panel._poll_status()
        assert "connection lost" in panel._status_label.text()
        assert len(_warnings(caplog, "connection lost")) == 1
        assert not panel._set_button.isEnabled()
        clock.advance(10.25)
        panel._poll_status()
        assert not panel._slot_change_in_flight
        wheel.broken = False
        wheel.finish_move()
        panel._poll_status()
        assert panel._status_label.text().startswith("Filter: 2")
        assert panel._set_button.isEnabled()
        panel.stop()

    def test_mount_park_panel(self, qapp: object, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock()
        mount = _BreakablePark(start_parked=True)
        panel = MountParkPanel(mount, clock=clock)
        panel._connect_button.setChecked(True)
        mount.stuck = True
        panel._on_unpark()
        mount.broken = True
        with caplog.at_level(logging.WARNING):
            panel._poll_status()
            panel._poll_status()
        assert "UTC" in panel._status_label.text()
        assert len(_warnings(caplog, "UTC")) == 1
        assert not panel._park_button.isEnabled() and not panel._unpark_button.isEnabled()
        clock.advance(30.25)
        panel._poll_status()
        assert not panel._action_in_flight
        mount.broken = False
        panel._poll_status()
        assert panel._status_label.text().startswith("Parked")
        assert panel._unpark_button.isEnabled()
        panel.stop()


class TestFilterWheelAlert:
    """S4 finding (b): the driver's Alert for an out-of-range slot reaches the operator."""

    def test_an_alert_ends_the_slot_change_at_once_with_the_rejection_shown(
        self, qapp: object
    ) -> None:
        clock = FakeClock()
        adapter, wheel = make_simulated_filter_wheel_adapter(
            FilterWheelScenario("ToupTek EFW 2", slot_names=("L", "R", "G"), initial_slot=2),
            clock=clock,
        )
        panel = FilterWheelPanel(adapter, selectable=True, clock=clock)
        panel._connect_button.setChecked(True)
        panel._poll_status()
        panel._slot_combo.addItem("9", 9)  # a slot the wheel does not have
        panel._slot_combo.setCurrentIndex(panel._slot_combo.findData(9))
        issued_at = clock.monotonic()
        panel._on_set_clicked()
        assert wheel.state == "Alert"
        panel._poll_status()
        assert panel._slot_change_in_flight  # within the adapter's 1 s stale-Alert grace
        clock.advance(1.0)
        panel._poll_status()
        assert clock.monotonic() - issued_at < 10.0  # not the 10 s safety net
        assert not panel._slot_change_in_flight
        assert "rejected" in panel._status_label.text()
        assert panel._set_button.isEnabled()
        panel.stop()
