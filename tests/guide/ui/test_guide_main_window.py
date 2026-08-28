import time

from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from guide_tool.ui.main_window import MainWindow


def test_window_starts_idle(qapp: object) -> None:
    window = MainWindow(FakeCamera())
    assert window._status_label.text() == "Start guiding to begin."
    assert not window._start_button.isChecked()


def test_starting_guiding_and_polling_shows_healthy_status_and_frame(qapp: object) -> None:
    window = MainWindow(FakeCamera())
    window._start_button.setChecked(True)
    try:
        deadline = time.monotonic() + 2.0
        while "Health: healthy" not in window._status_label.text():
            assert time.monotonic() < deadline, "guiding never reported healthy in time"
            time.sleep(0.02)
            window._poll_status()

        assert not window._live_view.pixmap().isNull()
    finally:
        window._start_button.setChecked(False)


def test_stopping_guiding_updates_the_label(qapp: object) -> None:
    window = MainWindow(FakeCamera())
    window._start_button.setChecked(True)
    window._start_button.setChecked(False)
    assert window._status_label.text() == "Stopped."


class TestCameraSelection:
    def test_combo_offers_only_demo_camera_when_no_devices_found(self, qapp: object) -> None:
        window = MainWindow(FakeCamera(), device_lister=lambda: [])
        assert window._camera_combo.count() == 1
        assert window._camera_combo.currentData() is None

    def test_combo_lists_enumerated_touptek_devices(self, qapp: object) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="OAG Guide Cam")]
        window = MainWindow(FakeCamera(), device_lister=lambda: devices)
        assert window._camera_combo.count() == 2
        assert "OAG Guide Cam" in window._camera_combo.itemText(1)

    def test_connect_rebuilds_the_controller_around_the_selected_camera(
        self, qapp: object
    ) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="OAG Guide Cam")]
        fake_touptek = FakeTouptekCamera()
        window = MainWindow(
            FakeCamera(),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: fake_touptek,
        )
        original_controller = window._controller
        window._camera_combo.setCurrentIndex(1)
        window._on_connect_camera()
        assert window._controller is not original_controller
        assert "OAG Guide Cam" in window._camera_status_label.text()

    def test_connect_failure_shows_error_and_keeps_current_controller(
        self, qapp: object
    ) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="OAG Guide Cam")]
        window = MainWindow(
            FakeCamera(),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(fail_connect=True),
        )
        original_controller = window._controller
        window._camera_combo.setCurrentIndex(1)
        window._on_connect_camera()
        assert window._controller is original_controller
        assert "failed" in window._camera_status_label.text().lower()

    def test_camera_controls_disabled_while_guiding(self, qapp: object) -> None:
        window = MainWindow(FakeCamera(), device_lister=lambda: [])
        window._start_button.setChecked(True)
        assert not window._camera_combo.isEnabled()
        assert not window._connect_button.isEnabled()
        window._start_button.setChecked(False)
        assert window._camera_combo.isEnabled()
        assert window._connect_button.isEnabled()
