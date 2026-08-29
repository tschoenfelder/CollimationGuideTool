import json
import time
from pathlib import Path

from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo
from astrotool_core.diagnostics import DiagnosticService
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


class TestDiagnosticsCapture:
    """See GitHub issue #10."""

    def test_manual_capture_writes_a_bundle_and_shows_the_uuid(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        window = MainWindow(FakeCamera(), device_lister=lambda: [], diagnostics=diagnostics)
        window._diagnostics_note.setText("guide correction seemed backwards")
        window._on_capture_diagnostics()

        bundles = list(tmp_path.iterdir())
        assert len(bundles) == 1
        assert bundles[0].name in window._diagnostics_status_label.text()
        incident = json.loads((bundles[0] / "incident.json").read_text(encoding="utf-8"))
        assert incident["app"] == "GuideTool"
        assert incident["trigger"] == "manual"
        assert incident["reason"] == "guide correction seemed backwards"

    def test_capture_includes_guiding_status_and_a_recent_frame(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        window = MainWindow(FakeCamera(), device_lister=lambda: [], diagnostics=diagnostics)
        window._start_button.setChecked(True)
        try:
            deadline = time.monotonic() + 2.0
            while "Health: healthy" not in window._status_label.text():
                assert time.monotonic() < deadline, "guiding never reported healthy in time"
                time.sleep(0.02)
                window._poll_status()
        finally:
            window._start_button.setChecked(False)

        window._on_capture_diagnostics()
        bundle_dir = next(tmp_path.iterdir())
        incident = json.loads((bundle_dir / "incident.json").read_text(encoding="utf-8"))
        context = incident["context"]
        assert "guiding_status" in context
        assert "serial_number" in context["camera_descriptor"]
        frames_dir = bundle_dir / "frames"
        assert frames_dir.is_dir()
        assert len(list(frames_dir.iterdir())) >= 1

    def test_capture_failure_shows_an_error_and_does_not_raise(
        self, qapp: object, tmp_path: Path
    ) -> None:
        unwritable = tmp_path / "some" / "file.txt"
        unwritable.parent.mkdir()
        unwritable.write_text("not a directory")
        diagnostics = DiagnosticService(app_name="GuideTool", diagnostics_dir=unwritable)
        window = MainWindow(FakeCamera(), device_lister=lambda: [], diagnostics=diagnostics)
        window._on_capture_diagnostics()  # must not raise
        assert "failed" in window._diagnostics_status_label.text().lower()

    def test_context_tracks_camera_across_a_connect_swap(
        self, qapp: object, tmp_path: Path
    ) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="OAG Guide Cam")]
        fake_touptek = FakeTouptekCamera()
        diagnostics = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            FakeCamera(),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: fake_touptek,
            diagnostics=diagnostics,
        )
        window._camera_combo.setCurrentIndex(1)
        window._on_connect_camera()

        window._on_capture_diagnostics()
        bundle_dir = next(tmp_path.iterdir())
        incident = json.loads((bundle_dir / "incident.json").read_text(encoding="utf-8"))
        assert (
            incident["context"]["camera_descriptor"]["serial_number"]
            == fake_touptek.get_descriptor().serial_number
        )
