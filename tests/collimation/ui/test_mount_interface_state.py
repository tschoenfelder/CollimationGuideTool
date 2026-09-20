"""Issue #42: Mount Align's status text, Run Calibration and the nudge pad all
derive from ONE mount-interface availability state, and a stale
"Mount interface not available" message never outlives the condition.

Field bundle 6dbe7d2c: the mount was connected and unparked (interface
available) yet Mount Align still said "mount interface not available" while
Run Calibration was enabled -- the message was only ever SET, never cleared."""

from __future__ import annotations

import numpy as np
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.mount.no_mount import NoMountAdapter
from astrotool_core.mount.port import MountPort
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.ui.main_window import MainWindow
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel


def _panel(
    park: FakeMountPark, pulse: MountPort | None = None
) -> tuple[MountTestMovePanel, FakeMountPark]:
    panel = MountTestMovePanel(
        pulse if pulse is not None else FakeMountAdapter(),
        mount_park=park,
        get_left_frame=lambda: np.zeros((60, 80), dtype=np.float32),
        get_right_frame=lambda: np.zeros((60, 80), dtype=np.float32),
    )
    return panel, park


class TestOneSourceOfTruth:
    def test_not_connected_disables_everything_with_a_reason_code(self, qapp: object) -> None:
        panel, _ = _panel(FakeMountPark(start_parked=False))

        state = panel.interface_state()

        assert not state.available and state.code == "not_connected"
        assert not panel._run_calibration_button.isEnabled()

    def test_connected_with_both_interfaces_is_available_and_enables_run(
        self, qapp: object
    ) -> None:
        panel, _ = _panel(FakeMountPark(start_parked=False))

        panel._connect_button.setChecked(True)

        assert panel.interface_state().available
        assert panel.interface_state().code == "ok"
        assert panel._run_calibration_button.isEnabled()
        panel.stop()

    def test_the_park_interface_missing_disables_run_and_says_why(self, qapp: object) -> None:
        panel, _ = _panel(FakeMountPark(available=False))

        panel._connect_button.setChecked(True)

        state = panel.interface_state()
        assert state.code == "park_interface_unavailable"
        assert not panel._run_calibration_button.isEnabled()
        assert "not available" in panel._result_label.text().lower()
        assert "park" in panel._result_label.text().lower()  # the concrete reason
        panel.stop()

    def test_the_pulse_interface_missing_disables_run_and_says_why(self, qapp: object) -> None:
        panel, _ = _panel(FakeMountPark(start_parked=False), NoMountAdapter())

        panel._connect_button.setChecked(True)

        assert panel.interface_state().code == "pulse_interface_unavailable"
        assert not panel._run_calibration_button.isEnabled()
        assert "not available" in panel._result_label.text().lower()
        panel.stop()

    def test_status_text_and_button_state_are_never_contradictory(self, qapp: object) -> None:
        park = FakeMountPark(available=False)
        panel, _ = _panel(park)
        panel._connect_button.setChecked(True)

        for available in (False, True, False, True):
            park._available = available
            panel._update_buttons_enabled()
            says_unavailable = "not available" in panel._result_label.text().lower()
            assert panel._run_calibration_button.isEnabled() == (not says_unavailable)
            assert panel._run_calibration_button.isEnabled() == panel.interface_state().available
        panel.stop()


class TestPropagation:
    def test_the_interface_appearing_later_clears_the_stale_message(self, qapp: object) -> None:
        """The field scenario: Mount Align was connected first, the Mount panel
        (park interface) came up afterwards."""
        park = FakeMountPark(available=False)
        panel, _ = _panel(park)
        panel._connect_button.setChecked(True)
        assert "not available" in panel._result_label.text().lower()

        park.make_available()
        panel._update_buttons_enabled()  # what the poll timer does

        assert panel._run_calibration_button.isEnabled()
        assert "not available" not in panel._result_label.text().lower()
        panel.stop()

    def test_disconnect_and_reconnect_follow_without_a_restart(self, qapp: object) -> None:
        park = FakeMountPark(start_parked=False)
        panel, _ = _panel(park)
        panel._connect_button.setChecked(True)
        assert panel._run_calibration_button.isEnabled()

        park._available = False  # the mount goes away
        panel._update_buttons_enabled()
        assert not panel._run_calibration_button.isEnabled()
        assert "not available" in panel._result_label.text().lower()

        park.make_available()  # ... and comes back
        panel._update_buttons_enabled()
        assert panel._run_calibration_button.isEnabled()
        assert "not available" not in panel._result_label.text().lower()
        panel.stop()

    def test_disconnecting_mount_align_clears_the_availability_message(self, qapp: object) -> None:
        panel, _ = _panel(FakeMountPark(available=False))
        panel._connect_button.setChecked(True)
        assert "not available" in panel._result_label.text().lower()

        panel._connect_button.setChecked(False)

        assert "not available" not in panel._result_label.text().lower()
        assert not panel._run_calibration_button.isEnabled()

    def test_a_real_result_message_is_never_stomped(self, qapp: object) -> None:
        panel, _ = _panel(FakeMountPark(start_parked=False))
        panel._connect_button.setChecked(True)
        panel._result_label.setText("Main: RA-axis 12.0 px | Dec-axis 3.0 px")

        panel._update_buttons_enabled()

        assert panel._result_label.text() == "Main: RA-axis 12.0 px | Dec-axis 3.0 px"
        panel.stop()

    def test_a_real_result_message_survives_the_mount_going_unavailable_then_back(
        self, qapp: object
    ) -> None:
        park = FakeMountPark(start_parked=False)
        panel, _ = _panel(park)
        panel._connect_button.setChecked(True)
        park._available = False
        panel._update_buttons_enabled()
        panel._result_label.setText("Calibration failed: something specific")

        park.make_available()
        panel._update_buttons_enabled()

        assert panel._result_label.text() == "Calibration failed: something specific"
        panel.stop()


class TestNudgePadFollowsTheSameState:
    def test_nudge_buttons_follow_the_shared_availability(self, qapp: object) -> None:
        park = FakeMountPark(start_parked=False)
        panel, _ = _panel(park)
        panel._connect_button.setChecked(True)
        nudges = [b for row in panel._nudge_buttons.values() for b in row.values()]
        assert all(b.isEnabled() for b in nudges)

        park._available = False
        panel._update_buttons_enabled()

        assert not any(b.isEnabled() for b in nudges)
        panel.stop()


class TestDiagnostics:
    def test_diagnostics_record_why_the_interface_is_or_is_not_available(
        self, qapp: object
    ) -> None:
        park = FakeMountPark(available=False)
        panel, _ = _panel(park)
        panel._connect_button.setChecked(True)

        info = panel.diagnostic_context()["mount_interface"]

        assert info["available"] is False
        assert info["code"] == "park_interface_unavailable"
        assert info["connected"] is True
        assert info["park_available"] is False
        assert info["pulse_connected"] is True
        assert info["park_adapter"] == "FakeMountPark"
        assert info["pulse_adapter"] == "FakeMountAdapter"
        panel.stop()


class TestOwnership:
    def test_mount_panel_and_mount_align_share_the_same_park_instance(self, qapp: object) -> None:
        image = np.full((60, 80), 100.0, dtype=np.float32)
        park = FakeMountPark(start_parked=False)
        window = MainWindow(
            ReplayCamera.from_arrays([image], cycle=True),
            device_lister=lambda: [],
            mount=park,
            pulse_mount=FakeMountAdapter(),
        )

        assert window._mount_panel._mount is window._test_move_panel._mount_park
        assert window._test_move_panel._mount_park is park  # no duplicate adapter
