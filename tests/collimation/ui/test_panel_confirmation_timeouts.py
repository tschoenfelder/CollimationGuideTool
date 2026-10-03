"""Confirmation-timeout safety nets of FocuserPanel, FilterWheelPanel and
MountParkPanel (issue #53, S3b).

Each panel disables its controls when it issues a command and re-enables them
when the device confirms -- or, as a safety net, once the command has been
outstanding for longer than its confirmation timeout (focuser 10 s, filter
wheel 10 s, park/unpark 30 s). The Qt poll timer stays the tick source; only
the time those polls read is injectable.

- `TestCharacterizationBeforeS3b` was written and run green BEFORE the clock
  injection, on the real clock (issued-at stamps aged by hand, as the
  existing panel tests do). It runs unchanged afterwards: neutrality proof.
- `TestFakeClock*` inject a `FakeClock` and pin the boundary exactly: a
  command outstanding for exactly the timeout is still waited for (the check
  is `elapsed > timeout`), a moment later it is released.
"""

from __future__ import annotations

import time

import pytest
from astrotool_core.filter_wheel.fake_filter_wheel import FakeFilterWheel
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.timing import SYSTEM_CLOCK, FakeClock
from collimation_tool.ui import filter_wheel_panel as fw_module
from collimation_tool.ui import focuser_panel as focuser_module
from collimation_tool.ui import mount_park_panel as park_module
from collimation_tool.ui.filter_wheel_panel import FilterWheelPanel
from collimation_tool.ui.focuser_panel import FocuserPanel
from collimation_tool.ui.mount_park_panel import MountParkPanel


def _stuck_wheel_panel(**kwargs: object) -> tuple[FilterWheelPanel, FakeFilterWheel]:
    wheel = FakeFilterWheel(slot=1, filter_names={1: "L", 2: "R"})
    panel = FilterWheelPanel(wheel, selectable=True, **kwargs)  # type: ignore[arg-type]
    panel._connect_button.setChecked(True)
    panel._poll_status()
    panel._slot_combo.setCurrentIndex(1)
    panel._on_set_clicked()
    panel._poll_status()  # observes Busy; finish_move() is never called -> stuck Busy
    assert panel._slot_change_in_flight
    return panel, wheel


def _moved_focuser_panel(**kwargs: object) -> FocuserPanel:
    # FakeFocuser completes instantly and never reports Busy, so only the
    # confirmation timeout can release the in-flight lock.
    panel = FocuserPanel(FakeFocuser(), **kwargs)  # type: ignore[arg-type]
    panel._connect_button.setChecked(True)
    panel._on_move_out()
    panel._poll_status()
    assert panel._move_in_flight
    return panel


def _unparked_park_panel(**kwargs: object) -> MountParkPanel:
    # FakeMountPark settles instantly, so the panel never sees a "not yet settled"
    # reading and only the 30 s safety net releases the buttons.
    panel = MountParkPanel(FakeMountPark(start_parked=True), **kwargs)  # type: ignore[arg-type]
    panel._connect_button.setChecked(True)
    panel._on_unpark()
    panel._poll_status()
    assert panel._action_in_flight
    return panel


class TestCharacterizationBeforeS3b:
    """Real clock; the issued-at stamp is aged to just under / just over the timeout."""

    def test_focuser_lock_is_held_inside_the_timeout_and_released_after_it(
        self, qapp: object
    ) -> None:
        panel = _moved_focuser_panel()
        timeout = focuser_module._MOVE_CONFIRMATION_TIMEOUT_S
        panel._move_issued_at = time.monotonic() - (timeout - 2.0)
        panel._poll_status()
        assert panel._move_in_flight
        assert not panel._in_button.isEnabled()
        panel._move_issued_at = time.monotonic() - (timeout + 2.0)
        panel._poll_status()
        assert not panel._move_in_flight
        assert panel._in_button.isEnabled()
        panel.stop()

    def test_filter_wheel_stuck_busy_is_held_inside_the_timeout_and_released_after_it(
        self, qapp: object
    ) -> None:
        panel, _wheel = _stuck_wheel_panel()
        timeout = fw_module._SLOT_CHANGE_CONFIRMATION_TIMEOUT_S
        panel._slot_change_issued_at = time.monotonic() - (timeout - 2.0)
        panel._poll_status()
        assert panel._slot_change_in_flight and not panel._set_button.isEnabled()
        panel._slot_change_issued_at = time.monotonic() - (timeout + 2.0)
        panel._poll_status()
        assert not panel._slot_change_in_flight
        assert panel._set_button.isEnabled()
        assert panel._requested_slot is None

    def test_park_panel_unconfirmed_transition_is_released_only_after_the_timeout(
        self, qapp: object
    ) -> None:
        panel = _unparked_park_panel()
        timeout = park_module._TRANSITION_CONFIRMATION_TIMEOUT_S
        panel._action_issued_at = time.monotonic() - (timeout - 2.0)
        panel._poll_status()
        assert panel._action_in_flight and not panel._park_button.isEnabled()
        panel._action_issued_at = time.monotonic() - (timeout + 2.0)
        panel._poll_status()
        assert not panel._action_in_flight
        assert panel._park_button.isEnabled()  # unparked now -> Park offered
        panel.stop()


class TestFakeClockBoundaries:
    """`elapsed > timeout`: exactly at the timeout the command is still awaited, any time
    after it the safety net releases the controls. The Qt timer is not involved: each
    `_poll_status()` call stands for one tick."""

    def test_the_panels_default_to_the_real_clock(self, qapp: object) -> None:
        focuser = FocuserPanel(FakeFocuser())
        wheel = FilterWheelPanel(FakeFilterWheel())
        park = MountParkPanel(FakeMountPark())
        assert focuser._clock is wheel._clock is park._clock is SYSTEM_CLOCK
        focuser.stop()
        park.stop()

    @pytest.mark.parametrize(
        ("waited_s", "released"),
        [(9.5, False), (10.0, False), (10.25, True)],
        ids=["before", "at", "after"],
    )
    def test_focuser_move_lock(self, qapp: object, waited_s: float, released: bool) -> None:
        clock = FakeClock(start=100.0)
        panel = _moved_focuser_panel(clock=clock)
        assert panel._move_issued_at == 100.0
        clock.advance(waited_s)
        panel._poll_status()
        assert panel._move_in_flight is not released
        assert panel._in_button.isEnabled() is released
        panel.stop()

    @pytest.mark.parametrize(
        ("waited_s", "released"),
        [(9.5, False), (10.0, False), (10.25, True)],
        ids=["before", "at", "after"],
    )
    def test_filter_wheel_stuck_busy(self, qapp: object, waited_s: float, released: bool) -> None:
        clock = FakeClock(start=100.0)
        panel, wheel = _stuck_wheel_panel(clock=clock)
        clock.advance(waited_s)
        panel._poll_status()
        assert wheel.status().moving  # still stuck Busy throughout
        assert panel._slot_change_in_flight is not released
        assert panel._set_button.isEnabled() is released

    @pytest.mark.parametrize(
        ("waited_s", "released"),
        [(29.5, False), (30.0, False), (30.25, True)],
        ids=["before", "at", "after"],
    )
    def test_park_panel_transition(self, qapp: object, waited_s: float, released: bool) -> None:
        clock = FakeClock(start=100.0)
        panel = _unparked_park_panel(clock=clock)
        clock.advance(waited_s)
        panel._poll_status()
        assert panel._action_in_flight is not released
        assert panel._park_button.isEnabled() is released
        panel.stop()

    def test_a_confirmed_move_never_waits_for_the_clock(self, qapp: object) -> None:
        """Device confirmation (Busy seen, then idle) releases at once; time only matters
        for the safety net."""
        clock = FakeClock()
        wheel = FakeFilterWheel(slot=1, filter_names={1: "L", 2: "R"})
        panel = FilterWheelPanel(wheel, selectable=True, clock=clock)
        panel._connect_button.setChecked(True)
        panel._poll_status()
        panel._slot_combo.setCurrentIndex(1)
        panel._on_set_clicked()
        panel._poll_status()  # Busy
        wheel.finish_move()
        panel._poll_status()  # Ok
        assert not panel._slot_change_in_flight
        assert clock.monotonic() == 0.0
