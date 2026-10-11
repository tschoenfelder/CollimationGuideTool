import json
import sys
import time
from pathlib import Path

import pytest
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo
from astrotool_core.diagnostics import DiagnosticService
from astrotool_core.testing import SimulatedToupcamSdk, install_simulated_toupcam
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from astrotool_core.timing import FakeClock
from guide_tool.ui.main_window import MainWindow
from PySide6.QtGui import QCloseEvent


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

    def test_status_field_is_read_only_and_selectable_not_a_plain_label(
        self, qapp: object
    ) -> None:
        """See issue #11 — the UUID must be copy/paste-able, not just readable."""
        window = MainWindow(FakeCamera(), device_lister=lambda: [])
        assert window._diagnostics_status_label.isReadOnly()

    def test_copy_button_copies_the_incident_uuid_to_the_clipboard(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        window = MainWindow(FakeCamera(), device_lister=lambda: [], diagnostics=diagnostics)
        window._on_capture_diagnostics()
        shown_uuid = window._diagnostics_status_label.text()

        window._on_copy_diagnostics_status()

        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        assert clipboard.text() == shown_uuid

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


class _TimeoutOnConnectCamera(FakeTouptekCamera):
    """A vendor SDK error that is not a ConnectionError (the 7bb7b68 bug class)."""

    def connect(self) -> None:
        raise TimeoutError("camera enumeration timed out")


class TestCameraSwitchLifecycle:
    """#52 K01/K03 (S6.2) on the simulated ToupTek SDK: GuideTool caught only ConnectionError
    on Connect and never released the previous camera (the #45 ERROR_BUSY class CameraPanel
    fixed: the SDK allows one open handle per device)."""

    @staticmethod
    def _window(
        monkeypatch: pytest.MonkeyPatch,
    ) -> tuple[MainWindow, SimulatedToupcamSdk]:
        sdk = SimulatedToupcamSdk(clock=FakeClock())
        install_simulated_toupcam(monkeypatch, sdk)
        sdk.add_camera("GPCMOS02000KPA", device_id="cam-a")
        sdk.add_camera("G3M678M", device_id="cam-b")
        devices = [
            TouptekDeviceInfo(index=0, camera_id="cam-a", display_name="GPCMOS02000KPA"),
            TouptekDeviceInfo(index=1, camera_id="cam-b", display_name="G3M678M"),
        ]
        return MainWindow(FakeCamera(), device_lister=lambda: devices), sdk

    @staticmethod
    def _connect(window: MainWindow, combo_index: int) -> None:
        window._camera_combo.setCurrentIndex(combo_index)
        window._on_connect_camera()

    def test_switching_to_another_camera_releases_the_previous_handle(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window, sdk = self._window(monkeypatch)
        self._connect(window, 1)
        handle_a = sdk.handles[-1]

        self._connect(window, 2)

        assert handle_a.closed
        assert not sdk.handles[-1].closed and sdk.handles[-1].device.id == "cam-b"
        assert "G3M678M" in window._camera_status_label.text()

    def test_reconnecting_the_same_camera_releases_the_old_handle_first(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window, sdk = self._window(monkeypatch)
        self._connect(window, 1)
        first = sdk.handles[-1]

        self._connect(window, 1)

        assert first.closed and len(sdk.handles) == 2 and not sdk.handles[-1].closed

    def test_choosing_the_demo_camera_releases_the_real_one(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window, sdk = self._window(monkeypatch)
        self._connect(window, 1)

        self._connect(window, 0)

        assert sdk.handles[-1].closed

    def test_a_non_connectionerror_failure_is_shown_and_the_previous_camera_restored(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        window, sdk = self._window(monkeypatch)
        self._connect(window, 1)
        controller_a = window._controller
        monkeypatch.setattr(window, "_camera_factory", lambda _id: _TimeoutOnConnectCamera())

        with caplog.at_level("WARNING"):
            self._connect(window, 2)  # must not raise

        assert "connect failed" in window._camera_status_label.text().lower()
        assert "camera enumeration timed out" in window._camera_status_label.text()
        assert window._controller is controller_a
        assert not sdk.handles[-1].closed and sdk.handles[-1].device.id == "cam-a"  # re-opened
        logged = [r for r in caplog.records if "camera enumeration timed out" in r.getMessage()]
        assert len(logged) == 1

    def test_quitting_releases_the_camera(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window, sdk = self._window(monkeypatch)
        self._connect(window, 1)

        window.closeEvent(QCloseEvent())

        assert sdk.handles[-1].closed

    def test_a_failing_release_on_quit_does_not_skip_stopping_the_controller(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window, _ = self._window(monkeypatch)
        self._connect(window, 1)
        monkeypatch.setattr(
            window._current_camera, "disconnect", lambda: (_ for _ in ()).throw(OSError("gone"))
        )
        stops: list[None] = []
        monkeypatch.setattr(window._controller, "stop", lambda: stops.append(None))

        window.closeEvent(QCloseEvent())  # must not raise

        assert stops == [None]


class TestInternalErrorsStillProduceAnIncident:
    """S6.2 review: a programming error in the Connect path used to escape to sys.excepthook,
    where the app's #10 hook writes an incident bundle. Contained now, it must still do so."""

    def test_an_internal_error_on_connect_is_captured_as_a_bundle(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from guide_tool.main import _install_excepthook

        monkeypatch.setattr(sys, "excepthook", lambda *_args: None)  # restored after the test
        diagnostics = DiagnosticService(app_name="GuideTool", diagnostics_dir=tmp_path)
        _install_excepthook(diagnostics)
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="OAG Guide Cam")]

        def broken_factory(_camera_id: str) -> FakeTouptekCamera:
            raise AttributeError("'NoneType' object has no attribute 'Toupcam'")

        window = MainWindow(
            FakeCamera(),
            device_lister=lambda: devices,
            camera_factory=broken_factory,
            diagnostics=diagnostics,
        )
        window._camera_combo.setCurrentIndex(1)

        window._on_connect_camera()  # must not raise

        assert "Internal error" in window._camera_status_label.text()
        bundles = list(tmp_path.iterdir())
        assert len(bundles) == 1
        incident = json.loads((bundles[0] / "incident.json").read_text(encoding="utf-8"))
        assert "AttributeError" in json.dumps(incident)
