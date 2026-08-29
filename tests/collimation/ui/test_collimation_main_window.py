import json
import time
from pathlib import Path

from astrotool_core.camera.port import CameraPort
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo
from astrotool_core.diagnostics import DiagnosticService
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


class TestDiagnosticsCapture:
    """See GitHub issue #10."""

    def test_manual_capture_writes_a_bundle_and_shows_the_uuid(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        window._diagnostics_note.setText("donut looked lopsided")
        window._on_capture_diagnostics()

        bundles = list(tmp_path.iterdir())
        assert len(bundles) == 1
        assert bundles[0].name in window._diagnostics_status_label.text()
        incident = json.loads((bundles[0] / "incident.json").read_text(encoding="utf-8"))
        assert incident["trigger"] == "manual"
        assert incident["reason"] == "donut looked lopsided"
        # The note field is cleared after a successful capture.
        assert window._diagnostics_note.text() == ""

    def test_manual_capture_without_a_note_uses_a_default_reason(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        window._on_capture_diagnostics()

        bundle_dir = next(tmp_path.iterdir())
        incident = json.loads((bundle_dir / "incident.json").read_text(encoding="utf-8"))
        assert incident["reason"]

    def test_capture_includes_camera_settings_and_last_measurement(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((5.0, -2.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        window._start_button.setChecked(True)
        try:
            deadline = time.monotonic() + 2.0
            while window._last_result is None:
                assert time.monotonic() < deadline, "no measurement observed in time"
                time.sleep(0.02)
                window._poll_frame()
        finally:
            window._start_button.setChecked(False)

        window._on_capture_diagnostics()
        bundle_dir = next(tmp_path.iterdir())
        incident = json.loads((bundle_dir / "incident.json").read_text(encoding="utf-8"))
        context = incident["context"]
        assert "serial_number" in context["camera_descriptor"]
        assert "measurement_result" in context
        # At least one recent frame was captured as raw FITS evidence.
        frames_dir = bundle_dir / "frames"
        assert frames_dir.is_dir()
        assert len(list(frames_dir.iterdir())) >= 1

    def test_recent_frame_buffer_is_bounded(self, qapp: object, tmp_path: Path) -> None:
        expected_capacity = 3
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        window._start_button.setChecked(True)
        try:
            deadline = time.monotonic() + 2.0
            while len(window._recent_frames) < expected_capacity:
                assert time.monotonic() < deadline, "buffer never filled"
                time.sleep(0.02)
                window._poll_frame()
            for _ in range(10):
                window._poll_frame()
        finally:
            window._start_button.setChecked(False)
        assert len(window._recent_frames) == expected_capacity

    def test_capture_failure_shows_an_error_and_does_not_raise(
        self, qapp: object, tmp_path: Path
    ) -> None:
        unwritable = tmp_path / "some" / "file.txt"
        unwritable.parent.mkdir()
        unwritable.write_text("not a directory")
        diagnostics = DiagnosticService(
            app_name="CollimationTool", diagnostics_dir=unwritable
        )
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        window._on_capture_diagnostics()  # must not raise
        assert "failed" in window._diagnostics_status_label.text().lower()

    def test_capture_diagnostics_is_available_by_default_without_injection(
        self, qapp: object
    ) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        assert window._diagnostics is not None
