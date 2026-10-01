"""Issue #51: the production `IndiFilterWheelAdapter` over the simulated INDI
filter wheel (`astrotool_core.testing.sim_indi_filter_wheel`) on fake time --
slots, invalid slot, Busy forever / delayed completion, disconnect while
moving, device-name variants, connect timeouts. No socket, no real sleep."""

from __future__ import annotations

import pytest
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.testing import (
    FilterWheelScenario,
    SimulatedIndiFilterWheel,
    make_simulated_filter_wheel_adapter,
)
from astrotool_core.timing import FakeClock

_NAME = "ToupTek EFW 2"
_SLOTS = ("L", "R", "G", "B", "Ha")


def _wheel(
    clock: FakeClock, **overrides: object
) -> tuple[IndiFilterWheelAdapter, SimulatedIndiFilterWheel]:
    scenario = FilterWheelScenario(
        **{"device_name": _NAME, "slot_names": _SLOTS, "initial_slot": 2, **overrides}  # type: ignore[arg-type]
    )
    return make_simulated_filter_wheel_adapter(scenario, clock=clock)


class TestSlotsAndNames:
    def test_slot_count_and_names_come_from_the_device(self) -> None:
        adapter, _ = _wheel(FakeClock())
        adapter.connect()
        assert adapter.slot_names() == dict(enumerate(_SLOTS, start=1))
        assert adapter.status().filter_name == "R"

    def test_eight_slot_wheel_without_published_names_uses_the_configuration(self) -> None:
        adapter, _ = make_simulated_filter_wheel_adapter(
            FilterWheelScenario(_NAME, slot_names=("",) * 8, publishes_names=False),
            clock=FakeClock(),
            filter_names={1: "L", 8: "NONE"},
        )
        adapter.connect()
        assert adapter.status().filter_name == "L"
        assert adapter.slot_names() == {1: "L", 8: "NONE"}

    def test_a_wheel_less_driver_connects_but_reports_no_wheel(self) -> None:
        clock = FakeClock()
        adapter, _ = _wheel(clock, wheel_present=False)
        adapter.connect()
        assert adapter.status().reason == "no filter wheel detected"
        # the 3 s hardware probe ran on fake time, not real time
        assert clock.monotonic() == pytest.approx(3.0, abs=0.06)


class TestMoveTiming:
    def test_busy_until_the_last_slot_of_travel_then_ok(self) -> None:
        clock = FakeClock()
        adapter, wheel = _wheel(clock, seconds_per_slot=0.5)
        adapter.connect()
        adapter.set_slot(4)  # two slots of travel: 1.0 s
        assert adapter.status().moving is True
        clock.advance(0.75)
        assert adapter.status().moving is True
        clock.advance(0.25)
        status = adapter.status()
        assert (status.moving, status.current_slot, status.filter_name) == (False, 4, "B")
        assert wheel.commanded_slots == [4]

    def test_reply_latency_delays_even_the_busy_state(self) -> None:
        """Property update delay: right after the command the client still
        sees the old Ok state -- the window #47's "never treat the requested
        slot as current" rule exists for."""
        clock = FakeClock()
        adapter, _ = _wheel(clock, reply_latency_s=0.25)
        adapter.connect()  # the connect itself waits on fake time for the replies
        adapter.set_slot(3)
        assert (adapter.status().moving, adapter.status().current_slot) == (False, 2)
        clock.advance(0.25)
        # libindi keeps the OLD value while Busy (see sim_indi_filter_wheel docstring)
        assert (adapter.status().moving, adapter.status().current_slot) == (True, 2)
        clock.advance(0.5)
        assert (adapter.status().moving, adapter.status().current_slot) == (False, 3)


class TestBusyValue:
    def test_libindi_keeps_the_old_slot_while_busy(self) -> None:
        clock = FakeClock()
        adapter, _ = _wheel(clock)
        adapter.connect()
        adapter.set_slot(4)
        status = adapter.status()
        assert (status.moving, status.current_slot, status.filter_name) == (True, 2, "R")

    def test_a_driver_echoing_the_target_while_busy(self) -> None:
        clock = FakeClock()
        adapter, _ = _wheel(clock, busy_reports_target=True)
        adapter.connect()
        adapter.set_slot(4)
        assert (adapter.status().moving, adapter.status().current_slot) == (True, 4)


class TestStuckBusy:
    """Diagnostic 73c7d59d: the real EFW kept reporting Busy."""

    def test_busy_forever_stays_moving_and_refuses_a_second_move(self) -> None:
        clock = FakeClock()
        adapter, _ = _wheel(clock, busy_forever=True)
        adapter.connect()
        adapter.set_slot(5)
        clock.advance(3600.0)
        assert adapter.status().moving is True
        with pytest.raises(RuntimeError, match="already in progress"):
            adapter.set_slot(1)


class TestInvalidSlot:
    def test_zero_is_refused_before_anything_is_sent(self) -> None:
        adapter, wheel = _wheel(FakeClock())
        adapter.connect()
        with pytest.raises(ValueError, match="invalid slot"):
            adapter.set_slot(0)
        assert wheel.commanded_slots == []

    def test_a_slot_beyond_the_wheel_is_answered_with_alert_and_never_moves(self) -> None:
        """libindi answers an out-of-range FILTER_SLOT with Alert. Characterizes
        a gap (recorded as a #51 finding): FilterWheelState has no notion of
        the rejection -- it reads "not moving, still at slot 2", so a caller
        only learns of it from its own confirmation timeout."""
        clock = FakeClock()
        adapter, wheel = _wheel(clock)
        adapter.connect()
        adapter.set_slot(9)
        assert wheel.commanded_slots == [9]
        assert wheel.state == "Alert"
        status = adapter.status()
        assert (status.moving, status.current_slot, status.reason) == (False, 2, None)


class TestDisconnectWhileMoving:
    def test_the_connection_dropping_mid_move_leaves_a_stale_busy_cache(self) -> None:
        """Characterization (recorded as a #51 finding, not fixed here):
        status() trusts the adapter's own `_connected` flag, not the client's,
        so after indiserver goes away mid-move it keeps reporting the last
        cached Busy forever instead of "not connected"."""
        clock = FakeClock()
        adapter, wheel = _wheel(clock)
        adapter.connect()
        adapter.set_slot(5)
        clock.call_later(0.5, wheel.drop_connection)
        clock.advance(10.0)
        assert adapter._client.is_connected is False
        status = adapter.status()
        assert status.moving is True and status.available is True
        assert wheel.slot == 5  # the wheel itself arrived; the app never heard

    def test_a_command_after_the_drop_raises_connection_error(self) -> None:
        clock = FakeClock()
        adapter, wheel = _wheel(clock)
        adapter.connect()
        wheel.drop_connection()
        with pytest.raises(ConnectionError):
            adapter.set_slot(3)


class TestConnectVariants:
    def test_wrong_configured_device_name_times_out_on_fake_time(self) -> None:
        """#47: the adapter configured as "ToupTek EFW 1" against a driver that
        is really "ToupTek EFW 2" -- a clean ConnectionError after exactly the
        configured timeout, with the client closed (no half-open state)."""
        clock = FakeClock()
        adapter, _ = make_simulated_filter_wheel_adapter(
            FilterWheelScenario(_NAME, slot_names=_SLOTS),
            clock=clock,
            device_name="ToupTek EFW 1",
            connect_timeout_s=10.0,
        )
        with pytest.raises(ConnectionError, match="'ToupTek EFW 1' did not confirm CONNECTION"):
            adapter.connect()
        assert clock.monotonic() == pytest.approx(10.0, abs=0.06)
        assert adapter._client.is_connected is False
        assert adapter.is_available is False

    def test_a_driver_that_never_confirms_connection_times_out(self) -> None:
        clock = FakeClock()
        adapter, _ = make_simulated_filter_wheel_adapter(
            FilterWheelScenario(_NAME, confirms_connection=False),
            clock=clock,
            connect_timeout_s=2.5,
        )
        with pytest.raises(ConnectionError, match="did not confirm CONNECTION within 2.5s"):
            adapter.connect()
        assert 2.5 <= clock.monotonic() < 2.6

    def test_an_unreachable_indiserver_fails_at_once(self) -> None:
        clock = FakeClock()
        adapter, _ = _wheel(clock, server_reachable=False)
        with pytest.raises(ConnectionError, match="could not connect to indiserver"):
            adapter.connect()
        assert clock.monotonic() == 0.0


class TestExternalChange:
    def test_a_driver_pushed_change_is_seen_without_any_command(self) -> None:
        clock = FakeClock()
        adapter, wheel = _wheel(clock)
        adapter.connect()
        wheel.external_move(5)
        assert adapter.status().current_slot == 5
