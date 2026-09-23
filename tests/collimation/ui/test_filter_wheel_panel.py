"""Tests for FilterWheelPanel -- issue #34: read-only EFW status
display. Construct the panel directly (project convention: avoids the
known Qt-teardown segfault odds a MainWindow-based test raises)."""

from __future__ import annotations

from astrotool_core.filter_wheel.fake_filter_wheel import FakeFilterWheel
from collimation_tool.ui.filter_wheel_panel import FilterWheelPanel


def _connected_panel(filter_wheel: FakeFilterWheel) -> FilterWheelPanel:
    panel = FilterWheelPanel(filter_wheel)
    panel._connect_button.setChecked(True)
    return panel


class TestStatusText:
    def test_not_connected_shows_not_connected(self, qapp: object) -> None:
        panel = FilterWheelPanel(FakeFilterWheel())
        assert panel._status_label.text() == "Filter: not connected"

    def test_nominal_slot_shows_slot_and_name(self, qapp: object) -> None:
        panel = _connected_panel(FakeFilterWheel(slot=3, filter_name="OIII"))
        panel._poll_status()
        assert panel._status_label.text() == "Filter: 3 — OIII"

    def test_slot_without_a_configured_name_shows_slot_only(self, qapp: object) -> None:
        panel = _connected_panel(FakeFilterWheel(slot=2, filter_name=None))
        panel._poll_status()
        assert panel._status_label.text() == "Filter: 2"

    def test_moving_shows_moving_to_prefix(self, qapp: object) -> None:
        panel = _connected_panel(FakeFilterWheel(slot=3, filter_name="OIII", moving=True))
        panel._poll_status()
        assert panel._status_label.text() == "Filter: moving to 3 — OIII"

    def test_no_wheel_detected_shows_explicit_state(self, qapp: object) -> None:
        panel = _connected_panel(FakeFilterWheel(available=False))
        panel._poll_status()
        assert panel._status_label.text() == "Filter: no filter wheel detected"

    def test_unreadable_position_shows_unknown(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=3)
        panel = _connected_panel(filter_wheel)
        filter_wheel._slot = None  # type: ignore[assignment]  # simulate an unreadable read
        panel._poll_status()
        assert panel._status_label.text() == "Filter: unknown"


class TestDiagnosticContext:
    def test_reports_the_full_state_shape(self, qapp: object) -> None:
        panel = _connected_panel(FakeFilterWheel(slot=3, filter_name="OIII", moving=True))
        context = panel.diagnostic_context()
        assert context == {
            "available": True,
            "current_slot": 3,
            "filter_name": "OIII",
            "moving": True,
            "reason": None,
            "requested_slot": None,
        }


class TestStop:
    def test_stop_is_safe_whether_or_not_connected(self, qapp: object) -> None:
        FilterWheelPanel(FakeFilterWheel()).stop()  # must not raise

        panel = _connected_panel(FakeFilterWheel())
        panel.stop()  # must not raise
        assert panel._connected is False


def _connected_selectable_panel(filter_wheel: FakeFilterWheel) -> FilterWheelPanel:
    panel = FilterWheelPanel(filter_wheel, selectable=True)
    panel._connect_button.setChecked(True)
    return panel


class TestSelectableVisibility:
    """Issue #47: the selector only exists on a panel main_window.py has
    told to be selectable -- selectable=False (the default) stays exactly
    #34's pure status display."""

    def test_not_selectable_hides_the_selector(self, qapp: object) -> None:
        panel = _connected_panel(FakeFilterWheel())
        assert panel._slot_combo.isHidden()
        assert panel._set_button.isHidden()

    def test_selectable_shows_the_selector(self, qapp: object) -> None:
        panel = _connected_selectable_panel(FakeFilterWheel())
        assert not panel._slot_combo.isHidden()
        assert not panel._set_button.isHidden()


class TestComboPopulation:
    def test_combo_entries_use_the_wheels_slot_names(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=2, filter_names={1: "L", 2: "R", 3: "G"})
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()

        labels = [panel._slot_combo.itemText(i) for i in range(panel._slot_combo.count())]
        assert labels == ["1 — L", "2 — R", "3 — G"]

    def test_a_slot_with_no_known_name_shows_the_bare_number(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=5, filter_names={})
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()

        labels = [panel._slot_combo.itemText(i) for i in range(panel._slot_combo.count())]
        assert labels == ["5"]  # the current slot is always included even if unnamed

    def test_a_provisional_selection_survives_repeated_poll_ticks(self, qapp: object) -> None:
        """Real-field report: picking an entry (e.g. OIII) kept resetting
        back to whatever the combo defaults to (its first entry) before a
        "Set" click could land, because the combo was unconditionally
        cleared and repopulated on every single poll tick -- including
        while nothing about the wheel's own names had changed at all."""
        filter_wheel = FakeFilterWheel(
            slot=1, filter_names={1: "Red", 2: "Green", 6: "OIII"}
        )
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()
        oiii_index = panel._slot_combo.findData(6)
        assert oiii_index >= 0
        panel._slot_combo.setCurrentIndex(oiii_index)

        for _ in range(5):
            panel._poll_status()  # the wheel's own state never changes here
            assert panel._slot_combo.currentData() == 6

    def test_the_combo_is_not_rebuilt_when_nothing_changed(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=1, filter_names={1: "L", 2: "R"})
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()
        first_signature = panel._combo_signature

        panel._poll_status()
        panel._poll_status()

        assert panel._combo_signature is first_signature


class TestOneActionAtATime:
    def test_clicking_set_disables_the_selector_synchronously(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=1, filter_names={1: "L", 2: "R"})
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()
        panel._slot_combo.setCurrentIndex(1)  # slot 2

        panel._on_set_clicked()

        assert not panel._slot_combo.isEnabled()
        assert not panel._set_button.isEnabled()
        assert filter_wheel.status().moving is True

    def test_a_second_click_while_in_flight_is_ignored(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=1, filter_names={1: "L", 2: "R"})
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()
        panel._slot_combo.setCurrentIndex(1)
        panel._on_set_clicked()

        panel._on_set_clicked()  # ignored -- _slot_change_in_flight guards it

        assert filter_wheel._pending_slot == 2  # only ever commanded once  # noqa: SLF001

    def test_arrival_reenables_the_selector(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=1, filter_names={1: "L", 2: "R"})
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()
        panel._slot_combo.setCurrentIndex(1)
        panel._on_set_clicked()
        panel._poll_status()  # observes Busy

        filter_wheel.finish_move()
        panel._poll_status()  # observes Busy -> Ok

        assert panel._slot_combo.isEnabled()
        assert panel._set_button.isEnabled()
        assert panel._status_label.text() == "Filter: 2 — R"


class TestRequestedTargetText:
    def test_shows_the_requested_slot_while_moving(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=1, filter_names={1: "L", 2: "R"})
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()
        panel._slot_combo.setCurrentIndex(1)
        panel._on_set_clicked()

        panel._poll_status()

        assert "requested 2" in panel._status_label.text()


class TestSetSlotFailure:
    def test_a_refused_move_is_shown_not_swallowed(self, qapp: object) -> None:
        filter_wheel = FakeFilterWheel(slot=1, filter_names={1: "L", 2: "R"}, moving=True)
        panel = _connected_selectable_panel(filter_wheel)
        panel._poll_status()
        panel._slot_combo.setCurrentIndex(1)

        panel._on_set_clicked()

        assert "already in progress" in panel._status_label.text()
        assert panel._slot_combo.isEnabled()  # recoverable, not stuck disabled
        assert panel._set_button.isEnabled()
