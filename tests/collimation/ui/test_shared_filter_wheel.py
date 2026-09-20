"""Issue #41 in the app: ONE Connect control per physical filter wheel, showing
which optical trains use it; none for a train without a wheel; failures are
explicit; and main.py really builds the wheel (the original bug: it never did)."""

from __future__ import annotations

import numpy as np
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.filter_wheel.fake_filter_wheel import FakeFilterWheel
from astrotool_core.filter_wheel.port import FilterWheelPort, FilterWheelState
from astrotool_core.filter_wheel.registry import FilterWheelAssignment
from collimation_tool.ui.filter_wheel_panel import FilterWheelPanel
from collimation_tool.ui.main_window import MainWindow
from PySide6.QtWidgets import QPushButton


def _window(assignments: list[FilterWheelAssignment] | None) -> MainWindow:
    image = np.full((60, 80), 100.0, dtype=np.float32)
    demo = ReplayCamera.from_arrays([image], cycle=True)
    return MainWindow(demo, device_lister=lambda: [], filter_wheels=assignments)


def _shared(port: FilterWheelPort | None = None) -> FilterWheelAssignment:
    return FilterWheelAssignment(
        wheel_id="efw1",
        device_name="ToupTek EFW 1",
        trains=("Main", "OAG"),
        port=port if port is not None else FakeFilterWheel(slot=3, filter_name="OIII"),
    )


class _RaisingWheel(FakeFilterWheel):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    def connect(self) -> None:
        raise self._error


class TestOneControlPerPhysicalWheel:
    def test_main_and_oag_sharing_a_wheel_get_exactly_one_connect_button(
        self, qapp: object
    ) -> None:
        window = _window([_shared()])

        panels = window._filter_wheel_panels
        assert len(panels) == 1
        buttons = [b for p in panels for b in p.findChildren(QPushButton)]
        assert len(buttons) == 1

    def test_the_panel_says_which_trains_use_the_wheel(self, qapp: object) -> None:
        window = _window([_shared()])

        panel = window._filter_wheel_panels[0]

        assert "ToupTek EFW 1" in panel._title_label.text()
        assert panel._used_by_label.text() == "Used by: Main, OAG"

    def test_guide_without_a_wheel_gets_no_control(self, qapp: object) -> None:
        window = _window([_shared()])

        assert all("Guide" not in p._used_by for p in window._filter_wheel_panels)
        assert window._guide_filter_wheel_panel is None

    def test_no_wheel_configured_means_no_panel_at_all(self, qapp: object) -> None:
        window = _window([])

        assert window._filter_wheel_panels == []

    def test_two_distinct_wheels_give_two_independent_controls(self, qapp: object) -> None:
        second = FilterWheelAssignment("efw2", "ToupTek EFW 2", ("Guide",), FakeFilterWheel(slot=1))
        window = _window([_shared(), second])

        panels = window._filter_wheel_panels
        assert len(panels) == 2
        panels[0]._connect_button.setChecked(True)
        assert panels[0]._connected and not panels[1]._connected


class TestSharedState:
    def test_connecting_shows_the_device_reported_slot_once_for_every_train(
        self, qapp: object
    ) -> None:
        window = _window([_shared()])
        panel = window._filter_wheel_panels[0]

        panel._connect_button.setChecked(True)

        assert "3" in panel._status_label.text() and "OIII" in panel._status_label.text()

    def test_a_slot_change_is_seen_by_main_and_oag_alike(self, qapp: object) -> None:
        wheel = FakeFilterWheel(slot=3, filter_name="OIII")
        window = _window([_shared(wheel)])
        window._filter_wheel_panels[0]._connect_button.setChecked(True)

        wheel._slot = 4
        wheel._filter_name = "Ha"
        window._filter_wheel_panels[0]._poll_status()

        context = window._diagnostic_context()
        assert context["main_filter_wheel"]["current_slot"] == 4
        assert context["oag_filter_wheel"]["current_slot"] == 4  # same wheel, same state
        assert context["main_filter_wheel"] == context["oag_filter_wheel"]

    def test_diagnostics_list_each_physical_wheel_with_its_trains(self, qapp: object) -> None:
        window = _window([_shared()])
        window._filter_wheel_panels[0]._connect_button.setChecked(True)

        wheels = window._diagnostic_context()["filter_wheels"]

        assert list(wheels) == ["efw1"]
        assert wheels["efw1"]["device"] == "ToupTek EFW 1"
        assert wheels["efw1"]["used_by"] == ["Main", "OAG"]
        assert wheels["efw1"]["connected"] is True

    def test_a_train_without_a_wheel_reports_none_assigned(self, qapp: object) -> None:
        window = _window([_shared()])

        context = window._diagnostic_context()

        assert context["guide_filter_wheel"]["available"] is False
        assert "no filter wheel assigned" in context["guide_filter_wheel"]["reason"]


class TestConnectFailuresAreExplicit:
    def test_a_connection_error_is_shown_not_swallowed(self, qapp: object) -> None:
        panel = FilterWheelPanel(_RaisingWheel(ConnectionError("no such device")))

        panel._connect_button.setChecked(True)

        assert "connect failed" in panel._status_label.text()
        assert "no such device" in panel._status_label.text()
        assert not panel._connected
        assert not panel._connect_button.isChecked()

    def test_an_os_error_from_a_dead_indiserver_is_shown_too(self, qapp: object) -> None:
        panel = FilterWheelPanel(_RaisingWheel(OSError("connection refused")))

        panel._connect_button.setChecked(True)  # must not raise into the Qt slot

        assert "connect failed" in panel._status_label.text()
        assert not panel._connected

    def test_a_stubborn_wheel_never_shows_connected(self, qapp: object) -> None:
        class _Down(FakeFilterWheel):
            def status(self) -> FilterWheelState:
                return FilterWheelState(False, None, None, False, "no filter wheel detected")

        panel = FilterWheelPanel(_Down())

        panel._connect_button.setChecked(True)

        assert "no filter wheel detected" in panel._status_label.text()


class TestLifecycle:
    def test_closing_the_window_disconnects_the_shared_wheel_once(self, qapp: object) -> None:
        class _Counting(FakeFilterWheel):
            disconnects = 0

            def disconnect(self) -> None:
                type(self).disconnects += 1

        wheel = _Counting()
        window = _window([_shared(wheel)])
        window._filter_wheel_panels[0]._connect_button.setChecked(True)

        window.close()

        assert _Counting.disconnects == 1  # one physical device, one disconnect


class TestMainWiresTheRealWheel:
    def test_main_builds_a_filter_wheel_for_the_real_device(self) -> None:
        """The original #41 bug: main.py never built a filter-wheel adapter,
        so every panel silently used NoFilterWheel."""
        from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
        from collimation_tool.main import _default_filter_wheels

        assignments = _default_filter_wheels()

        assert len(assignments) == 1
        assert isinstance(assignments[0].port, IndiFilterWheelAdapter)
        assert assignments[0].device_name == "ToupTek EFW 1"
        assert assignments[0].trains == ("Main", "OAG")
