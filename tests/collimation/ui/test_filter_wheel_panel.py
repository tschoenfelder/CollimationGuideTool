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
        }


class TestStop:
    def test_stop_is_safe_whether_or_not_connected(self, qapp: object) -> None:
        FilterWheelPanel(FakeFilterWheel()).stop()  # must not raise

        panel = _connected_panel(FakeFilterWheel())
        panel.stop()  # must not raise
        assert panel._connected is False
