"""Issue #44 in the application: ONE explicit operating mode (Terrestrial /
Astronomical) owns the mount-tracking policy; terrestrial => tracking OFF on
connect, on mode change, after movement, and every terrestrial measurement path
is blocked (fail closed, explicit reason) while tracking cannot be disabled."""

from __future__ import annotations

import numpy as np
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.mount.operating_mode import OperatingMode
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.ui.main_window import MainWindow


def _window(mount: FakeMountPark) -> MainWindow:
    image = np.full((60, 80), 100.0, dtype=np.float32)
    demo = ReplayCamera.from_arrays([image], cycle=True)
    return MainWindow(
        demo,
        device_lister=lambda: [],
        focuser=FakeFocuser(),
        mount=mount,
        pulse_mount=FakeMountAdapter(),
    )


def _tracking_mount(*, refuse_stop: bool = False) -> FakeMountPark:
    mount = FakeMountPark(start_parked=False, refuse_stop_tracking=refuse_stop)
    mount.start_tracking()
    return mount


class TestOperatingModeSelector:
    def test_default_mode_is_terrestrial_and_the_mount_align_target_follows(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))

        assert window._tracking_enforcer.mode is OperatingMode.TERRESTRIAL
        assert window._operating_terrestrial_button.isChecked()
        assert window._test_move_panel._terrestrial_button.isChecked()

    def test_switching_to_astronomical_lets_the_mount_align_target_follow(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))

        window._operating_astronomical_button.click()

        assert window._tracking_enforcer.mode is OperatingMode.ASTRONOMICAL
        assert window._test_move_panel._star_button.isChecked()
        assert window._star_field_mode_button.isChecked()

    def test_astronomical_to_terrestrial_forces_tracking_off(self, qapp: object) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        window._operating_astronomical_button.click()
        mount.start_tracking()
        assert mount.status().tracking is True

        window._operating_terrestrial_button.click()

        assert mount.status().tracking is False

    def test_terrestrial_to_astronomical_does_not_force_tracking(self, qapp: object) -> None:
        mount = FakeMountPark(start_parked=False)
        window = _window(mount)
        starts_before = mount.start_tracking_count

        window._operating_astronomical_button.click()

        assert mount.start_tracking_count == starts_before  # policy never forces ON

    def test_the_mode_switch_does_not_switch_away_an_artificial_star_registration_mode(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))
        window._artificial_star_mode_button.click()

        window._operating_terrestrial_button.click()

        assert window._artificial_star_mode_button.isChecked()


class TestConnectEnforcement:
    def test_connecting_the_mount_in_terrestrial_mode_turns_tracking_off(
        self, qapp: object
    ) -> None:
        mount = _tracking_mount()
        window = _window(mount)

        window._mount_panel._connect_button.setChecked(True)

        assert mount.status().tracking is False
        assert "tracking off" in window._mount_panel._status_label.text().lower()

    def test_connecting_in_astronomical_mode_leaves_tracking_alone(self, qapp: object) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        window._operating_astronomical_button.click()

        window._mount_panel._connect_button.setChecked(True)

        assert mount.status().tracking is True

    def test_tracking_that_cannot_be_disabled_is_shown_and_blocks_measurement(
        self, qapp: object
    ) -> None:
        mount = _tracking_mount(refuse_stop=True)
        window = _window(mount)

        window._mount_panel._connect_button.setChecked(True)

        text = window._mount_panel._status_label.text().lower()
        assert "blocked" in text and "tracking" in text
        assert window._tracking_enforcer.measurement_allowed() is False


class TestMeasurementPathsFailClosed:
    def test_fov_calibration_is_blocked_while_tracking_cannot_be_disabled(
        self, qapp: object
    ) -> None:
        window = _window(_tracking_mount(refuse_stop=True))

        window._calibrate_fov_button.click()

        text = window._calibrate_fov_status_label.text().lower()
        assert "blocked" in text and "tracking" in text

    def test_autofocus_is_blocked_while_tracking_cannot_be_disabled(self, qapp: object) -> None:
        window = _window(_tracking_mount(refuse_stop=True))

        window._focuser_panel._connect_button.setChecked(True)
        window._focuser_panel._on_auto_focus_clicked()

        text = window._focuser_panel._auto_focus_status_label.text().lower()
        assert "blocked" in text and "tracking" in text
        assert window._focuser_panel._autofocus_running is False

    def test_fine_collimation_is_blocked_while_tracking_cannot_be_disabled(
        self, qapp: object
    ) -> None:
        window = _window(_tracking_mount(refuse_stop=True))

        window._fine_collimation_panel._on_run_clicked()

        text = window._fine_collimation_panel._status_label.text().lower()
        assert "blocked" in text and "tracking" in text

    def test_measurement_paths_are_not_blocked_by_tracking_in_astronomical_mode(
        self, qapp: object
    ) -> None:
        window = _window(_tracking_mount(refuse_stop=True))
        window._operating_astronomical_button.click()

        window._calibrate_fov_button.click()

        assert "blocked" not in window._calibrate_fov_status_label.text().lower()

    def test_terrestrial_fov_calibration_turns_tracking_off_first(self, qapp: object) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        assert mount.status().tracking is True

        window._calibrate_fov_button.click()

        assert mount.status().tracking is False
        assert mount.start_tracking_count == 1  # only the fixture's own start


class TestMountAlignFollowsPolicy:
    def test_terrestrial_operating_mode_requires_tracking_off_even_if_star_is_toggled(
        self, qapp: object
    ) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        panel = window._test_move_panel
        panel._star_button.click()  # user flips the panel's own toggle

        failure = panel._verify_tracking_mode()

        assert failure is None
        assert mount.status().tracking is False  # policy wins: never enabled

    def test_a_blocked_policy_is_reported_by_mount_align(self, qapp: object) -> None:
        window = _window(_tracking_mount(refuse_stop=True))

        failure = window._test_move_panel._verify_tracking_mode()

        assert failure is not None and "tracking" in failure.lower()


class TestDiagnostics:
    def test_diagnostics_expose_operating_mode_and_the_tracking_trail(self, qapp: object) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        window._mount_panel._connect_button.setChecked(True)

        context = window._diagnostic_context()

        assert context["operating_mode"] == "terrestrial"
        tracking = context["tracking_policy"]
        assert tracking["operating_mode"] == "terrestrial"
        contexts = [step["context"] for step in tracking["transitions"]]
        assert "connect" in contexts
