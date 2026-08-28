import time

from astrotool_core.camera.port import CameraPort
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from astrotool_core.testing.frame_factory import donut_image
from collimation_tool.ui.main_window import MainWindow

_SHAPE = (240, 240)
_CENTER = (120.0, 120.0)


def _donut_camera(offset: tuple[float, float]) -> ReplayCamera:
    array = donut_image(
        _SHAPE,
        outer_center=_CENTER,
        outer_radius=50.0,
        inner_center=(_CENTER[0] + offset[0], _CENTER[1] + offset[1]),
        inner_radius=20.0,
        peak=3000.0,
        background=100.0,
    )
    return ReplayCamera.from_arrays([array], cycle=True)


def test_window_starts_idle(qapp: object) -> None:
    window = MainWindow(_donut_camera((0.0, 0.0)))
    assert window._recommendation_label.text() == "Start the stream to begin."
    assert not window._start_button.isChecked()


def test_starting_the_stream_and_polling_measures_and_shows_overlay(qapp: object) -> None:
    window = MainWindow(_donut_camera((5.0, -2.0)))
    window._start_button.setChecked(True)
    try:
        deadline = time.monotonic() + 2.0
        while "Error" not in window._recommendation_label.text():
            assert time.monotonic() < deadline, "no measurement observed in time"
            time.sleep(0.02)
            window._poll_frame()

        assert not window._live_view.pixmap().isNull()
    finally:
        window._start_button.setChecked(False)


def test_stopping_the_stream_updates_the_label(qapp: object) -> None:
    window = MainWindow(_donut_camera((0.0, 0.0)))
    window._start_button.setChecked(True)
    window._start_button.setChecked(False)
    assert window._recommendation_label.text() == "Stream stopped."
    assert window._stream is None


class TestCameraSelection:
    def test_combo_offers_only_demo_camera_when_no_devices_found(self, qapp: object) -> None:
        demo = _donut_camera((0.0, 0.0))
        window = MainWindow(demo, device_lister=lambda: [])
        assert window._camera_combo.count() == 1
        assert window._camera_combo.currentIndex() == 0
        assert window._camera_combo.currentData() is None

    def test_combo_lists_enumerated_touptek_devices(self, qapp: object) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")]
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: devices)
        assert window._camera_combo.count() == 2
        assert "ATR585M Guide" in window._camera_combo.itemText(1)
        assert window._camera_combo.itemData(1) == devices[0]

    def test_connect_swaps_to_the_selected_touptek_camera_on_success(self, qapp: object) -> None:
        demo = _donut_camera((0.0, 0.0))
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")]
        fake_touptek = FakeTouptekCamera()
        window = MainWindow(
            demo,
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: fake_touptek,
        )
        window._camera_combo.setCurrentIndex(1)
        window._on_connect_camera()
        assert window._camera is fake_touptek
        assert "ATR585M Guide" in window._camera_status_label.text()

    def test_connect_failure_shows_error_and_keeps_current_camera(self, qapp: object) -> None:
        demo = _donut_camera((0.0, 0.0))
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")]
        window = MainWindow(
            demo,
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(fail_connect=True),
        )
        window._camera_combo.setCurrentIndex(1)
        window._on_connect_camera()
        assert window._camera is demo
        assert "failed" in window._camera_status_label.text().lower()

    def test_reselecting_demo_camera_restores_it(self, qapp: object) -> None:
        demo = _donut_camera((0.0, 0.0))
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")]
        fake_touptek = FakeTouptekCamera()
        window = MainWindow(
            demo,
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: fake_touptek,
        )
        window._camera_combo.setCurrentIndex(1)
        window._on_connect_camera()
        current: CameraPort = window._camera
        assert current is fake_touptek

        window._camera_combo.setCurrentIndex(0)
        window._on_connect_camera()
        current = window._camera
        assert current is demo

    def test_camera_controls_disabled_while_streaming(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        window._start_button.setChecked(True)
        assert not window._camera_combo.isEnabled()
        assert not window._connect_button.isEnabled()
        window._start_button.setChecked(False)
        assert window._camera_combo.isEnabled()
        assert window._connect_button.isEnabled()
