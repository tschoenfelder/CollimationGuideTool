import json
import time
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from astropy.io import fits
from astrotool_core.acquisition.acquisition_state import AcquisitionState
from astrotool_core.acquisition.auto_exposure import AutoExposureConfig
from astrotool_core.camera.capabilities import CameraCapabilities
from astrotool_core.camera.port import CameraPort
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo
from astrotool_core.config import MountAlignmentSettings, load_camera_settings
from astrotool_core.diagnostics import DiagnosticService
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.port import FocuserStatus
from astrotool_core.frames.frame import Frame
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.park_port import MountParkPort, MountParkStatus
from astrotool_core.mount.port import AxisDirection, CommandResult, MountAxis, MountPort
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from astrotool_core.testing.frame_factory import donut_image, single_star_image
from collimation_tool.ui.camera_panel import CameraPanel
from collimation_tool.ui.focuser_panel import FocuserPanel
from collimation_tool.ui.main_window import MainWindow
from collimation_tool.ui.mount_park_panel import MountParkPanel
from collimation_tool.ui.mount_test_move_panel import _degenerate_calibration_message

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


def _star_camera(x: float, y: float) -> ReplayCamera:
    array = single_star_image((120, 120), x=x, y=y, peak=2000.0, sigma=2.5, background=100.0)
    return ReplayCamera.from_arrays([array], cycle=True)


def _textured_camera(seed: int) -> ReplayCamera:
    """Plain textured noise, not a star -- detect_sources() finds nothing
    in this (confirmed: 0 sources, "too dark"), standing in for ordinary
    terrestrial content Test Move's "Terrestrial" toggle is for (see
    MountTestMovePanel's own docstring, incident 6fa2aa59)."""
    rng = np.random.default_rng(seed)
    array = rng.normal(loc=500.0, scale=80.0, size=(120, 120))
    return ReplayCamera.from_arrays([array], cycle=True)


def _axis_response(axis: MountAxis, dx_px: float, dy_px: float) -> AxisResponse:
    return AxisResponse(
        axis=axis, direction=AxisDirection.POSITIVE, duration_ms=1000,
        dx_px=dx_px, dy_px=dy_px, px_per_ms=0.0,
    )


class TestDegenerateCalibrationMessage:
    """Direct unit coverage for _degenerate_calibration_message() -- the
    panel-level tests in TestMountTestMovePanel exercise it end-to-end
    through a real (fake) calibration run; these pin its actual wording
    per case without needing to drive a full Run Calibration each time."""

    def test_zero_on_both_cameras_gets_the_mount_cable_note(self) -> None:
        axis1 = _axis_response(MountAxis.AXIS1, 0.0, 0.0)
        axis2 = _axis_response(MountAxis.AXIS2, 0.0, 5.0)
        message = _degenerate_calibration_message(
            "left", axis1, axis2, {MountAxis.AXIS1: _axis_response(MountAxis.AXIS1, 0.0, 0.0)}
        )
        assert "RA-axis measured no motion on either camera" in message
        assert "mount/cable issue" in message

    def test_zero_here_but_real_on_the_other_camera_gets_the_framing_note(self) -> None:
        axis1 = _axis_response(MountAxis.AXIS1, 0.0, 0.0)
        axis2 = _axis_response(MountAxis.AXIS2, 5.0, 0.0)
        message = _degenerate_calibration_message(
            "left", axis1, axis2, {MountAxis.AXIS1: _axis_response(MountAxis.AXIS1, 3.0, 0.0)}
        )
        assert "RA-axis measured no motion here, but Guide confirms real motion" in message
        assert "framing/plate scale" in message

    def test_names_the_correct_other_camera_for_the_right_side(self) -> None:
        axis1 = _axis_response(MountAxis.AXIS1, 0.0, 0.0)
        axis2 = _axis_response(MountAxis.AXIS2, 5.0, 0.0)
        message = _degenerate_calibration_message(
            "right", axis1, axis2, {MountAxis.AXIS1: _axis_response(MountAxis.AXIS1, 3.0, 0.0)}
        )
        assert "Main confirms real motion" in message

    def test_missing_other_camera_data_falls_back_to_the_mount_cable_note(self) -> None:
        # No calibration_partial entry at all for the other camera's AXIS1
        # (e.g. that camera never even connected) -- must not crash, and
        # must not claim a confirmation that was never actually measured.
        axis1 = _axis_response(MountAxis.AXIS1, 0.0, 0.0)
        axis2 = _axis_response(MountAxis.AXIS2, 5.0, 0.0)
        message = _degenerate_calibration_message("left", axis1, axis2, {})
        assert "RA-axis measured no motion on either camera" in message


def test_window_starts_idle(qapp: object) -> None:
    window = MainWindow(_donut_camera((0.0, 0.0)))
    assert window._left_panel._recommendation_label.text() == "Start the stream to begin."
    assert not window._left_panel._start_button.isChecked()


def test_starting_the_stream_and_polling_measures_and_shows_overlay(qapp: object) -> None:
    window = MainWindow(_donut_camera((5.0, -2.0)))
    panel = window._left_panel
    panel._start_button.setChecked(True)
    try:
        deadline = time.monotonic() + 2.0
        while "Error" not in panel._recommendation_label.text():
            assert time.monotonic() < deadline, "no measurement observed in time"
            time.sleep(0.02)
            panel._poll_frame()

        assert not panel._live_view.pixmap().isNull()
    finally:
        panel._start_button.setChecked(False)


def test_stopping_the_stream_updates_the_label(qapp: object) -> None:
    window = MainWindow(_donut_camera((0.0, 0.0)))
    panel = window._left_panel
    panel._start_button.setChecked(True)
    panel._start_button.setChecked(False)
    assert panel._recommendation_label.text() == "Stream stopped."
    assert panel._stream is None


class TestCameraSelection:
    def test_combo_offers_only_demo_camera_when_no_devices_found(self, qapp: object) -> None:
        demo = _donut_camera((0.0, 0.0))
        window = MainWindow(demo, device_lister=lambda: [])
        combo = window._left_panel._camera_combo
        assert combo.count() == 1
        assert combo.currentIndex() == 0
        assert combo.currentData() is None

    def test_combo_lists_enumerated_touptek_devices(self, qapp: object) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")]
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: devices)
        combo = window._left_panel._camera_combo
        assert combo.count() == 2
        assert "ATR585M Guide" in combo.itemText(1)
        assert combo.itemData(1) == devices[0]

    def test_connect_swaps_to_the_selected_touptek_camera_on_success(self, qapp: object) -> None:
        demo = _donut_camera((0.0, 0.0))
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")]
        fake_touptek = FakeTouptekCamera()
        window = MainWindow(
            demo,
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: fake_touptek,
        )
        panel = window._left_panel
        panel._camera_combo.setCurrentIndex(1)
        panel._on_connect_camera()
        assert panel._camera is fake_touptek
        assert "ATR585M Guide" in panel._camera_status_label.text()

    def test_connect_failure_shows_error_and_keeps_current_camera(self, qapp: object) -> None:
        demo = _donut_camera((0.0, 0.0))
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")]
        window = MainWindow(
            demo,
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(fail_connect=True),
        )
        panel = window._left_panel
        panel._camera_combo.setCurrentIndex(1)
        panel._on_connect_camera()
        assert panel._camera is demo
        assert "failed" in panel._camera_status_label.text().lower()

    def test_reselecting_demo_camera_restores_it(self, qapp: object) -> None:
        demo = _donut_camera((0.0, 0.0))
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")]
        fake_touptek = FakeTouptekCamera()
        window = MainWindow(
            demo,
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: fake_touptek,
        )
        panel = window._left_panel
        panel._camera_combo.setCurrentIndex(1)
        panel._on_connect_camera()
        current: CameraPort = panel._camera
        assert current is fake_touptek

        panel._camera_combo.setCurrentIndex(0)
        panel._on_connect_camera()
        current = panel._camera
        assert current is demo

    def test_camera_controls_disabled_while_streaming(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._start_button.setChecked(True)
        assert not panel._camera_combo.isEnabled()
        assert not panel._connect_button.isEnabled()
        panel._start_button.setChecked(False)
        assert panel._camera_combo.isEnabled()
        assert panel._connect_button.isEnabled()


class TestTwoPanelExclusion:
    """See the two-camera-panel feature request: connecting a real device
    on one side must remove it from the other side's combo — a ToupTek
    camera only allows one open handle at a time."""

    def test_both_panels_offer_the_same_devices_before_any_connection(
        self, qapp: object
    ) -> None:
        devices = [
            TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M"),
            TouptekDeviceInfo(index=1, camera_id="dev-2", display_name="GPCMOS"),
        ]
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: devices)
        assert window._left_panel._camera_combo.count() == 3  # demo + 2
        assert window._right_panel._camera_combo.count() == 3

    def test_connecting_a_device_on_the_left_removes_it_from_the_right_combo(
        self, qapp: object
    ) -> None:
        devices = [
            TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M"),
            TouptekDeviceInfo(index=1, camera_id="dev-2", display_name="GPCMOS"),
        ]
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(),
        )
        window._left_panel._camera_combo.setCurrentIndex(1)  # dev-1
        window._left_panel._on_connect_camera()

        right_ids = {
            window._right_panel._camera_combo.itemData(i).camera_id
            for i in range(1, window._right_panel._camera_combo.count())
        }
        assert right_ids == {"dev-2"}

    def test_connecting_on_the_right_removes_it_from_the_left_combo(
        self, qapp: object
    ) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M")]
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(),
        )
        window._right_panel._camera_combo.setCurrentIndex(1)  # dev-1
        window._right_panel._on_connect_camera()

        assert window._left_panel._camera_combo.count() == 1  # demo only

    def test_switching_back_to_demo_restores_the_device_on_the_other_side(
        self, qapp: object
    ) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M")]
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(),
        )
        window._left_panel._camera_combo.setCurrentIndex(1)
        window._left_panel._on_connect_camera()
        assert window._right_panel._camera_combo.count() == 1  # excluded

        window._left_panel._camera_combo.setCurrentIndex(0)  # back to demo
        window._left_panel._on_connect_camera()
        assert window._right_panel._camera_combo.count() == 2  # demo + dev-1 again

    def test_both_panels_can_independently_select_the_demo_camera(
        self, qapp: object
    ) -> None:
        """The demo camera has no hardware-conflict constraint — both sides
        may use it at once."""
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        window._left_panel._on_connect_camera()
        window._right_panel._on_connect_camera()
        assert window._left_panel._camera_combo.currentData() is None
        assert window._right_panel._camera_combo.currentData() is None

    def test_exclusion_survives_a_device_lister_returning_fresh_instances(
        self, qapp: object
    ) -> None:
        """Regression test: the real list_devices() builds a brand new
        TouptekDeviceInfo on every call (not the same object each time,
        unlike this test file's usual `lambda: devices` fixtures) — found
        via real-hardware testing that refresh_camera_list()'s selection
        restore silently broke in exactly this situation because
        QComboBox.findData() doesn't reliably match by value for a custom
        Python object, only (sometimes) by identity."""

        def fresh_devices() -> list[TouptekDeviceInfo]:
            return [
                TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M"),
                TouptekDeviceInfo(index=1, camera_id="dev-2", display_name="GPCMOS"),
            ]

        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=fresh_devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(),
        )
        right = window._right_panel
        # Select (not yet connect) dev-2 on the right before the left side
        # triggers a refresh_camera_list() call.
        right_idx = next(
            i
            for i in range(right._camera_combo.count())
            if (data := right._camera_combo.itemData(i)) is not None
            and data.camera_id == "dev-2"
        )
        right._camera_combo.setCurrentIndex(right_idx)

        window._left_panel._camera_combo.setCurrentIndex(1)  # dev-1
        window._left_panel._on_connect_camera()

        current = right._camera_combo.currentData()
        assert current is not None
        assert current.camera_id == "dev-2"


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

    def test_status_field_is_read_only_and_selectable_not_a_plain_label(
        self, qapp: object
    ) -> None:
        """See issue #11 — the UUID must be copy/paste-able, not just readable."""
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        assert window._diagnostics_status_label.isReadOnly()

    def test_copy_button_copies_the_incident_uuid_to_the_clipboard(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        window._on_capture_diagnostics()
        shown_uuid = window._diagnostics_status_label.text()

        window._on_copy_diagnostics_status()

        from PySide6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        assert clipboard is not None
        assert clipboard.text() == shown_uuid

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

    def test_capture_includes_both_panels_camera_settings_and_measurement(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((5.0, -2.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        panel = window._left_panel
        panel._start_button.setChecked(True)
        try:
            deadline = time.monotonic() + 2.0
            while panel._last_result is None:
                assert time.monotonic() < deadline, "no measurement observed in time"
                time.sleep(0.02)
                panel._poll_frame()
        finally:
            panel._start_button.setChecked(False)

        window._on_capture_diagnostics()
        bundle_dir = next(tmp_path.iterdir())
        incident = json.loads((bundle_dir / "incident.json").read_text(encoding="utf-8"))
        context = incident["context"]
        assert "left" in context
        assert "right" in context
        assert "serial_number" in context["left"]["camera_descriptor"]
        assert "measurement_result" in context["left"]
        # At least one recent frame (from either panel) was captured as
        # raw FITS evidence.
        frames_dir = bundle_dir / "frames"
        assert frames_dir.is_dir()
        assert len(list(frames_dir.iterdir())) >= 1

    def test_capture_includes_the_displayed_image_for_a_streaming_panel(
        self, qapp: object, tmp_path: Path
    ) -> None:
        """See the real-world question this was added for: "Are you
        stored the frames display and the calibration result as well?"
        — the raw FITS in frames/ is unstretched sensor data with no
        overlay; images/*_display.png is what the operator actually saw."""
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((5.0, -2.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        panel = window._left_panel
        panel._start_button.setChecked(True)
        try:
            deadline = time.monotonic() + 2.0
            while panel._live_view._base_pixmap is None:
                assert time.monotonic() < deadline, "no frame ever displayed"
                time.sleep(0.02)
                panel._poll_frame()
        finally:
            panel._start_button.setChecked(False)

        window._on_capture_diagnostics()
        bundle_dir = next(tmp_path.iterdir())
        image_path = bundle_dir / "images" / "left_display.png"
        assert image_path.is_file()
        # A real PNG, not an empty/placeholder file.
        assert image_path.read_bytes().startswith(b"\x89PNG")

    def test_capture_omits_fov_calibration_context_when_none_has_run_yet(
        self, qapp: object, tmp_path: Path
    ) -> None:
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        window._on_capture_diagnostics()
        bundle_dir = next(tmp_path.iterdir())
        incident = json.loads((bundle_dir / "incident.json").read_text(encoding="utf-8"))
        assert "fov_calibration" not in incident["context"]

    def test_capture_includes_the_fov_calibration_result_once_one_has_run(
        self, qapp: object, tmp_path: Path
    ) -> None:
        guide = _starfield(80, 80, n_stars=30, seed=50)
        main_array = guide[20:60, 15:65].copy()
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            ReplayCamera.from_arrays([main_array], cycle=True),
            guide_camera=ReplayCamera.from_arrays([guide], cycle=True),
            device_lister=lambda: [],
            diagnostics=diagnostics,
            main_pixel_scale_arcsec=1.0,
            guide_pixel_scale_arcsec=1.0,
        )
        window._left_panel._start_button.setChecked(True)
        window._right_panel._start_button.setChecked(True)
        try:
            window._left_panel._poll_frame()
            window._right_panel._poll_frame()
        finally:
            window._left_panel._start_button.setChecked(False)
            window._right_panel._start_button.setChecked(False)

        window._on_calibrate_fov()
        deadline = time.monotonic() + 15.0
        while window._calibrate_fov_poll_timer.isActive():
            assert time.monotonic() < deadline, "calibration never completed"
            time.sleep(0.02)
            window._poll_fov_calibration()
        assert window._last_calibration_result is not None  # sanity: a match was found

        window._on_capture_diagnostics()
        bundle_dir = next(tmp_path.iterdir())
        incident = json.loads((bundle_dir / "incident.json").read_text(encoding="utf-8"))
        calibration = incident["context"]["fov_calibration"]
        assert calibration["rotation_deg"] == window._last_calibration_result.rotation_deg
        assert calibration["scale"] == window._last_calibration_result.scale
        assert calibration["score"] == window._last_calibration_result.score

    def test_recent_frame_buffer_is_bounded_per_panel(self, qapp: object, tmp_path: Path) -> None:
        expected_capacity = 3
        diagnostics = DiagnosticService(app_name="CollimationTool", diagnostics_dir=tmp_path)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], diagnostics=diagnostics
        )
        panel = window._left_panel
        panel._start_button.setChecked(True)
        try:
            deadline = time.monotonic() + 2.0
            while len(panel._recent_frames) < expected_capacity:
                assert time.monotonic() < deadline, "buffer never filled"
                time.sleep(0.02)
                panel._poll_frame()
            for _ in range(10):
                panel._poll_frame()
        finally:
            panel._start_button.setChecked(False)
        assert len(panel._recent_frames) == expected_capacity

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


class _CallOrderSpyCamera(ReplayCamera):
    """Records which of set_gain()/set_exposure_ms() was actually called
    first -- see _apply_auto_exposure's own comment (diagnostic 79bcc6a8)
    for why gain must go first. `call_order` is set by the test right
    after construction (via .from_arrays()), not here, to avoid
    reproducing ReplayCamera's own multi-parameter __init__."""

    call_order: list[str]

    def set_gain(self, gain: int) -> None:
        self.call_order.append("gain")
        super().set_gain(gain)

    def set_exposure_ms(self, ms: float) -> None:
        self.call_order.append("exposure")
        super().set_exposure_ms(ms)


class TestAutoExposure:
    """See the follow-up to issue #10: histogram-based auto exposure/gain."""

    def test_checkbox_off_by_default_and_manual_controls_enabled(
        self, qapp: object
    ) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        assert not panel._auto_exposure_checkbox.isChecked()
        assert panel._exposure_spin.isEnabled()
        assert panel._gain_spin.isEnabled()

    def test_enabling_disables_manual_spinboxes_and_resets_gain_to_default(
        self, qapp: object
    ) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._gain_spin.setValue(250)
        panel._auto_exposure_checkbox.setChecked(True)
        assert not panel._exposure_spin.isEnabled()
        assert not panel._gain_spin.isEnabled()
        assert panel._gain_spin.value() == 100
        assert panel._camera.get_gain() == 100

    def test_disabling_reenables_manual_controls(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._auto_exposure_checkbox.setChecked(True)
        panel._auto_exposure_checkbox.setChecked(False)
        assert panel._exposure_spin.isEnabled()
        assert panel._gain_spin.isEnabled()

    def test_a_dim_donut_frame_increases_exposure_while_streaming(
        self, qapp: object
    ) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        # ReplayCamera's own default (2000ms) happens to equal
        # AutoExposureConfig's default live-view ceiling — starting there
        # would correctly *not* raise exposure any further (it's already
        # at the ceiling), which isn't what this test means to exercise.
        panel._exposure_spin.setValue(10.0)
        initial_exposure = panel._exposure_spin.value()
        panel._auto_exposure_checkbox.setChecked(True)
        panel._start_button.setChecked(True)
        try:
            panel._poll_frame()
            time.sleep(0.05)
            panel._poll_frame()
        finally:
            panel._start_button.setChecked(False)
        # The demo donut's peak (3000) is a small fraction of 16-bit full
        # range — well below the 50-70% target band — so exposure must rise.
        assert panel._exposure_spin.value() > initial_exposure
        assert panel._gain_spin.value() == 100

    def test_no_change_applied_while_auto_exposure_is_off(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        initial_exposure = panel._exposure_spin.value()
        panel._start_button.setChecked(True)
        try:
            panel._poll_frame()
            time.sleep(0.05)
            panel._poll_frame()
        finally:
            panel._start_button.setChecked(False)
        assert panel._exposure_spin.value() == initial_exposure

    def test_custom_auto_exposure_config_default_gain_is_honored(
        self, qapp: object
    ) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
            auto_exposure_config=AutoExposureConfig(default_gain=250),
        )
        panel = window._left_panel
        panel._auto_exposure_checkbox.setChecked(True)
        assert panel._gain_spin.value() == 250

    def test_a_bright_uniform_frame_within_band_leaves_exposure_unchanged(
        self, qapp: object
    ) -> None:
        # A synthetic in-band frame (60% of 16-bit full range) via
        # ReplayCamera. Only the top ~50 (of 4096) pixels sit at that
        # value — enough that the 99th percentile still lands exactly on
        # it (virtual index 4054.05, well inside the top-50 block), but
        # far from "most pixels at their own max", which
        # auto_exposure's saturation-fraction check treats as genuine
        # hardware saturation regardless of the absolute value.
        array = np.zeros((64, 64), dtype=np.float32)
        array.flat[-50:] = 0.6 * 65535
        camera = ReplayCamera.from_arrays([array], cycle=True)
        window = MainWindow(camera, device_lister=lambda: [])
        panel = window._left_panel
        initial_exposure = panel._exposure_spin.value()
        panel._auto_exposure_checkbox.setChecked(True)
        panel._start_button.setChecked(True)
        try:
            panel._poll_frame()
        finally:
            panel._start_button.setChecked(False)
        assert panel._exposure_spin.value() == initial_exposure
        assert panel._gain_spin.value() == 100

    def test_exposure_never_climbs_past_the_live_view_ceiling(self, qapp: object) -> None:
        """Regression test for a real-hardware bug: exposure could climb
        from a tiny value past 10+ real seconds within a handful of poll
        cycles, then the camera blocked every capture for that long,
        freezing the live view. Exposure must stop climbing at
        AutoExposureConfig's default ceiling (2s) and switch to gain."""
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._exposure_spin.setValue(panel._auto_exposure_config.max_auto_exposure_ms)
        panel._auto_exposure_checkbox.setChecked(True)
        panel._start_button.setChecked(True)
        try:
            for _ in range(10):
                panel._poll_frame()
                time.sleep(0.01)
        finally:
            panel._start_button.setChecked(False)
        assert panel._exposure_spin.value() <= panel._auto_exposure_config.max_auto_exposure_ms
        assert panel._gain_spin.value() > 100

    def test_exposure_spin_can_represent_a_cameras_fractional_minimum_exactly(
        self, qapp: object
    ) -> None:
        """Regression test for diagnostic 79bcc6a8: with only 1 decimal,
        clamping to a real camera's own minimum (GPCMOS02000KPA's is
        0.105ms) silently rounded to 0.1ms -- below the camera's actual
        hardware floor, which the SDK then rejected."""
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._exposure_spin.setValue(0.105)
        assert panel._exposure_spin.value() == 0.105

    def test_gain_is_corrected_before_exposure_within_one_auto_exposure_step(
        self, qapp: object
    ) -> None:
        """Regression test for diagnostic 79bcc6a8: _apply_auto_exposure
        used to set exposure before gain. A real ToupTek SDK rejection of
        a rounded-off exposure value (HRESULTException/E_INVALIDARG, a
        few microseconds under the camera's true floor) turned out not
        to actually block the line after it -- a direct PySide6 probe
        confirmed a raising valueChanged slot is swallowed at Qt's
        signal-dispatch boundary, not propagated -- but that's an
        implementation detail of this Qt binding, not a guarantee. Gain
        must not depend on it: it's applied first, unconditionally."""
        # A saturated frame at minimum exposure drives both corrections
        # in one step -- see compute_auto_exposure's "too bright even at
        # minimum exposure" case.
        array = np.full((64, 64), 65534.0, dtype=np.float32)  # fully saturated, 16-bit
        # from_arrays() is typed to return the base ReplayCamera (it has
        # no Self-typed classmethod signature) even though it correctly
        # constructs this subclass at runtime -- cast narrows it back.
        camera = cast(_CallOrderSpyCamera, _CallOrderSpyCamera.from_arrays([array], cycle=True))
        camera.call_order = []
        # Above the camera's min_exposure_ms (0.1) so the correction
        # actually changes exposure too, not just gain -- see
        # compute_auto_exposure's "too bright even at minimum exposure"
        # case: 0.15 * 0.6 = 0.09, clamped up to the 0.1 floor.
        camera._exposure_ms = 0.15
        window = MainWindow(camera, device_lister=lambda: [])
        panel = window._left_panel
        panel._auto_exposure_checkbox.setChecked(True)
        panel._gain_spin.setValue(2060)
        camera.call_order.clear()  # drop the setup calls above
        panel._start_button.setChecked(True)
        try:
            panel._poll_frame()
        finally:
            panel._start_button.setChecked(False)
        assert camera.call_order == ["gain", "exposure"]
        assert panel._camera.get_gain() < 2060

    def test_previous_metric_and_gain_are_threaded_across_corrections(
        self, qapp: object
    ) -> None:
        """auto_exposure.py's adaptive gain step (see its "Gain step is
        adaptive" docstring section) needs the (metric, gain) pair from
        this panel's own *previous* correction, since compute_auto_exposure()
        is deliberately stateless -- the panel carries that state itself
        and must thread the latest pair through on every call, not just
        the first."""
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        assert panel._auto_exposure_previous_metric is None
        assert panel._auto_exposure_previous_gain is None

        # Exposure already at the live-view ceiling -- a dim-but-not-fully-
        # black frame triggers a *gain* correction immediately (a fully
        # black frame instead pins desired_exposure exactly at the
        # ceiling rather than past it -- see
        # test_fully_black_frame_pushes_exposure_toward_the_live_view_ceiling
        # in test_auto_exposure.py -- so this needs a tiny nonzero signal,
        # same shape as that file's own _frame() helper).
        panel._exposure_spin.setValue(panel._auto_exposure_config.max_auto_exposure_ms)
        dim_pixels = np.zeros((10, 10), dtype=np.float32)
        dim_pixels[-3:] = 1.0
        dim = Frame(
            pixels=dim_pixels, header=fits.Header(), exposure_seconds=0.001, bit_depth=16
        )

        panel._apply_auto_exposure(dim)
        assert panel._auto_exposure_previous_gain == 100  # the gain that produced `dim`
        assert panel._auto_exposure_previous_metric is not None
        # No prior pair yet -- falls back to the fixed step (10).
        assert panel._camera.get_gain() == 110

        panel._apply_auto_exposure(dim)
        # previous_gain must now reflect *this* call's own current_gain
        # (110), not still the very first call's (100).
        assert panel._auto_exposure_previous_gain == 110
        # Same flat frame again -> metric unchanged -> zero sensitivity ->
        # falls back to the fixed step again, same as the first call.
        assert panel._camera.get_gain() == 120

    def test_previous_metric_and_gain_reset_when_auto_exposure_is_freshly_enabled(
        self, qapp: object
    ) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._auto_exposure_previous_metric = 0.42
        panel._auto_exposure_previous_gain = 250

        panel._auto_exposure_checkbox.setChecked(True)

        assert panel._auto_exposure_previous_metric is None
        assert panel._auto_exposure_previous_gain is None


class _CaptureFailingCamera(ReplayCamera):
    """capture() always raises -- simulates StreamController's background
    capture thread dying permanently on a real hardware failure (see that
    class's own docstring; real incident 79bcc6a8's follow-up)."""

    def capture(self, exposure_seconds: float) -> Frame:
        raise RuntimeError("simulated capture failure")


class TestStreamErrorVisibility:
    """Regression tests for diagnostic 79bcc6a8's follow-up: a dead
    background capture thread used to be completely invisible --
    diagnostic_context()'s "streaming" flag kept reporting True forever,
    since it only checked `self._stream is not None`. See
    CameraPanel._handle_stream_error's own docstring."""

    def _wait_for_stream_error(self, panel: object, *, timeout_s: float = 5.0) -> None:
        stream = panel._stream  # type: ignore[attr-defined]
        assert stream is not None
        deadline = time.monotonic() + timeout_s
        while stream.state != AcquisitionState.ERROR:
            assert time.monotonic() < deadline, "capture thread never reported an error"
            time.sleep(0.01)

    def test_a_dead_capture_thread_is_surfaced_and_the_panel_resets(
        self, qapp: object
    ) -> None:
        camera = _CaptureFailingCamera.from_arrays(
            [np.zeros((8, 8), dtype=np.float32)], cycle=True
        )
        window = MainWindow(camera, device_lister=lambda: [])
        panel = window._left_panel
        panel._start_button.setChecked(True)
        self._wait_for_stream_error(panel)

        panel._poll_frame()

        assert panel._stream is None
        assert not panel._start_button.isChecked()
        assert panel._start_button.text() == "Start stream"
        assert panel._camera_combo.isEnabled()
        assert panel._connect_button.isEnabled()
        assert "error" in panel._recommendation_label.text().lower()
        assert "simulated capture failure" in panel._recommendation_label.text()
        assert panel.diagnostic_context()["stream_error"] == "simulated capture failure"
        assert panel.diagnostic_context()["streaming"] is False
        window.close()

    def test_no_stream_error_key_when_nothing_has_failed(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        assert "stream_error" not in window._left_panel.diagnostic_context()

    def test_a_fresh_stream_start_clears_a_previous_error(self, qapp: object) -> None:
        camera = _CaptureFailingCamera.from_arrays(
            [np.zeros((8, 8), dtype=np.float32)], cycle=True
        )
        window = MainWindow(camera, device_lister=lambda: [])
        panel = window._left_panel
        panel._start_button.setChecked(True)
        self._wait_for_stream_error(panel)
        panel._poll_frame()
        assert "stream_error" in panel.diagnostic_context()

        panel._start_button.setChecked(True)  # restart, still the same failing camera

        assert "stream_error" not in panel.diagnostic_context()
        window.close()


class TestRightPanel:
    """The right/guide panel is a fully independent CameraPanel — spot-check
    it works the same as the left, rather than repeating every case above."""

    def test_right_panel_defaults_to_a_fake_camera_when_not_supplied(
        self, qapp: object
    ) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        assert window._right_panel is not window._left_panel
        assert window._right_panel._camera is not window._left_panel._camera

    def test_explicit_guide_camera_is_used_for_the_right_panel(self, qapp: object) -> None:
        guide = _donut_camera((1.0, 1.0))
        window = MainWindow(
            _donut_camera((0.0, 0.0)), guide_camera=guide, device_lister=lambda: []
        )
        assert window._right_panel._camera is guide

    def test_right_panel_streams_and_measures_independently(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            guide_camera=_donut_camera((5.0, -2.0)),
            device_lister=lambda: [],
        )
        panel = window._right_panel
        panel._start_button.setChecked(True)
        try:
            deadline = time.monotonic() + 2.0
            while "Error" not in panel._recommendation_label.text():
                assert time.monotonic() < deadline, "no measurement observed in time"
                time.sleep(0.02)
                panel._poll_frame()
            assert not panel._live_view.pixmap().isNull()
        finally:
            panel._start_button.setChecked(False)
        # The left panel is untouched by the right panel streaming.
        assert window._left_panel._stream is None

    def test_closing_the_window_stops_both_panels(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            guide_camera=_donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
        )
        window._left_panel._start_button.setChecked(True)
        window._right_panel._start_button.setChecked(True)
        window.close()
        assert window._left_panel._stream is None
        assert window._right_panel._stream is None


def _camera_with_sensor(width_px: int, height_px: int) -> ReplayCamera:
    caps = CameraCapabilities(
        min_gain=100,
        max_gain=15000,
        min_exposure_ms=0.1,
        max_exposure_ms=3_600_000.0,
        supports_cooling=True,
        supports_hcg=True,
        supports_lcg=True,
        supports_hdr=True,
        supports_black_level=True,
        bit_depth=16,
        pixel_size_um=0.0,
        sensor_width_px=width_px,
        sensor_height_px=height_px,
    )
    return ReplayCamera.from_arrays(
        [np.zeros((height_px, width_px), dtype=np.float32)], capabilities=caps
    )


class TestFovOverlayIntegration:
    """Test below UI: the real rig this feature was built for —
    ATR585M (main, 3840x2160, 0.38"/px) and GPCMOS02000KPA (guide,
    1920x1080, ~3.32"/px) — end to end through MainWindow, not just the
    pure compute_fov_overlay_rect() math (see test_fov_overlay.py for
    that)."""

    _MAIN_SCALE = 0.38
    _GUIDE_SCALE = 3.32

    def test_overlay_computed_for_the_real_rig_specs_end_to_end(self, qapp: object) -> None:
        window = MainWindow(
            _camera_with_sensor(3840, 2160),
            guide_camera=_camera_with_sensor(1920, 1080),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=self._MAIN_SCALE,
            guide_pixel_scale_arcsec=self._GUIDE_SCALE,
        )
        rect = window._right_panel._fov_rect
        assert rect is not None
        assert rect.width == pytest.approx(0.2289, abs=0.001)
        assert rect.height == pytest.approx(0.2289, abs=0.001)
        assert rect.x == pytest.approx((1.0 - rect.width) / 2.0)

    def test_no_overlay_when_a_pixel_scale_is_explicitly_unavailable(
        self, qapp: object
    ) -> None:
        # Deliberately not relying on ~/.SmartTScope/config.toml being
        # absent (true on Windows/CI, but *not* true wherever SmartTScope
        # is actually installed alongside this project, e.g. the Pi this
        # feature was verified on). And deliberately *not* passing None
        # either — None means "unspecified, auto-detect from config" (see
        # MainWindow's constructor), so it can't be used to force "no
        # data" on a machine where the config genuinely exists; 0.0 is a
        # given-but-invalid value, exercised regardless of the machine.
        window = MainWindow(
            _camera_with_sensor(3840, 2160),
            guide_camera=_camera_with_sensor(1920, 1080),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=0.0,
            guide_pixel_scale_arcsec=0.0,
        )
        assert window._right_panel._fov_rect is None

    def test_default_construction_finds_real_data_when_smarttscope_is_installed(
        self, qapp: object
    ) -> None:
        # Verified on the Pi this feature was built for: with no override
        # given, MainWindow reads the real ~/.SmartTScope/config.toml and
        # produces the real rig's overlay — skipped everywhere else.
        from astrotool_core.optics import DEFAULT_CONFIG_PATH

        if not DEFAULT_CONFIG_PATH.is_file():
            pytest.skip("no ~/.SmartTScope/config.toml on this machine")
        window = MainWindow(
            _camera_with_sensor(3840, 2160),
            guide_camera=_camera_with_sensor(1920, 1080),
            device_lister=lambda: [],
        )
        rect = window._right_panel._fov_rect
        assert rect is not None
        assert rect.width == pytest.approx(0.2289, abs=0.001)

    def test_overlay_recomputes_when_a_panel_connects_a_differently_sized_camera(
        self, qapp: object
    ) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M")]
        window = MainWindow(
            _camera_with_sensor(1920, 1080),  # left starts the same size as guide
            guide_camera=_camera_with_sensor(1920, 1080),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: _camera_with_sensor(3840, 2160),
            main_pixel_scale_arcsec=self._MAIN_SCALE,
            guide_pixel_scale_arcsec=self._GUIDE_SCALE,
        )
        initial_rect = window._right_panel._fov_rect
        assert initial_rect is not None
        # Equal *pixel counts* on both sides initially, but the plate
        # scales still differ (0.38 vs 3.32"/px), so the ratio isn't 1.0.
        assert initial_rect.width == pytest.approx(self._MAIN_SCALE / self._GUIDE_SCALE)

        window._left_panel._camera_combo.setCurrentIndex(1)
        window._left_panel._on_connect_camera()

        updated_rect = window._right_panel._fov_rect
        assert updated_rect is not None
        assert updated_rect.width == pytest.approx(0.2289, abs=0.001)

    def test_overlay_is_visibly_rendered_in_the_guide_panels_live_view(
        self, qapp: object
    ) -> None:
        window = MainWindow(
            _camera_with_sensor(3840, 2160),
            guide_camera=_camera_with_sensor(1920, 1080),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=self._MAIN_SCALE,
            guide_pixel_scale_arcsec=self._GUIDE_SCALE,
        )
        panel = window._right_panel
        panel._start_button.setChecked(True)
        try:
            deadline = time.monotonic() + 2.0
            while panel._live_view._base_pixmap is None:
                assert time.monotonic() < deadline, "no frame rendered in time"
                time.sleep(0.02)
                panel._poll_frame()
        finally:
            panel._start_button.setChecked(False)

        assert panel._live_view._base_pixmap is not None
        image = panel._live_view._base_pixmap.toImage()
        rect = panel._fov_rect
        assert rect is not None
        # Middle of the rectangle's top edge, in native guide-frame pixels.
        x = int((rect.x + rect.width / 2) * 1920)
        y = int(rect.y * 1080)
        edge_color = image.pixelColor(x, y)
        assert edge_color.red() > 200
        assert edge_color.green() > 200
        assert edge_color.blue() < 100

    def test_overlay_stays_detectable_when_the_guide_camera_saturates(
        self, qapp: object
    ) -> None:
        """Below-UI regression for "guide stays black" (real hardware:
        GPCMOS02000KPA pinned at its true ~4094 ADC ceiling, tagged
        bit_depth=16). Two things must now hold end to end: auto-exposure
        must recognize the saturation and stop escalating gain (the real
        runaway was 100->380+), and the main camera's FOV rectangle must
        still be visible on screen — not washed into solid white, not
        hidden by a solid black misread of a saturated sensor."""
        guide_caps = CameraCapabilities(
            min_gain=100,
            max_gain=15000,
            min_exposure_ms=0.1,
            max_exposure_ms=3_600_000.0,
            supports_cooling=True,
            supports_hcg=True,
            supports_lcg=True,
            supports_hdr=True,
            supports_black_level=True,
            bit_depth=16,  # assumed — the sensor's true ADC ceiling is far lower
            pixel_size_um=2.9,
            sensor_width_px=1920,
            sensor_height_px=1080,
        )
        pinned = np.full((1080, 1920), 4094.0, dtype=np.float32)
        guide_camera = ReplayCamera.from_arrays([pinned], cycle=True, capabilities=guide_caps)
        window = MainWindow(
            _camera_with_sensor(3840, 2160),
            guide_camera=guide_camera,
            device_lister=lambda: [],
            main_pixel_scale_arcsec=self._MAIN_SCALE,
            guide_pixel_scale_arcsec=self._GUIDE_SCALE,
        )
        panel = window._right_panel
        panel._gain_spin.setValue(380)  # already escalated once, per the real report
        panel._auto_exposure_checkbox.setChecked(True)
        panel._start_button.setChecked(True)
        try:
            for _ in range(10):
                panel._poll_frame()
                time.sleep(0.01)
            gain_after_settling = panel._gain_spin.value()
            for _ in range(10):
                panel._poll_frame()
                time.sleep(0.01)
        finally:
            panel._start_button.setChecked(False)

        # Recognized as saturated, not "far too dim" — gain must not have
        # kept climbing past where it already was.
        assert panel._gain_spin.value() <= gain_after_settling

        assert panel._live_view._base_pixmap is not None
        image = panel._live_view._base_pixmap.toImage()
        # A background pixel, away from the overlay rectangle: a saturated
        # sensor must render bright (white), not black — detectable as
        # "something is there", just overexposed.
        background = image.pixelColor(10, 10)
        assert background.red() > 200

        rect = panel._fov_rect
        assert rect is not None
        x = int((rect.x + rect.width / 2) * 1920)
        y = int(rect.y * 1080)
        edge_color = image.pixelColor(x, y)
        # The main camera's FOV marker must still read as yellow, distinct
        # from the saturated-white background behind it (same red/green,
        # but background blue is high while the marker's is near zero).
        assert edge_color.blue() < 100


def _starfield(height: int, width: int, *, n_stars: int, seed: int) -> np.ndarray:
    """See tests/collimation/ui/test_fov_registration.py's identical
    helper — duplicated locally rather than imported, matching this
    file's existing convention of small self-contained test fixtures."""
    rng = np.random.default_rng(seed)
    image = np.full((height, width), 100.0, dtype=np.float32)
    ys, xs = np.meshgrid(np.arange(height), np.arange(width), indexing="ij")
    for _ in range(n_stars):
        cx, cy = rng.uniform(0, width), rng.uniform(0, height)
        peak = rng.uniform(500.0, 3000.0)
        sigma = rng.uniform(1.5, 3.0)
        image += peak * np.exp(-(((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sigma**2)))
    return image.astype(np.float32)


class TestFovCalibration:
    """See MainWindow's "Calibrate FOV" — content-matches the two
    panels' latest captured frames, replacing the config-only centered
    placeholder with a real (possibly rotated) match."""

    def test_status_message_when_no_frame_has_been_captured_yet(self, qapp: object) -> None:
        window = MainWindow(
            _camera_with_sensor(200, 200),
            guide_camera=_camera_with_sensor(200, 200),
            device_lister=lambda: [],
        )
        window._on_calibrate_fov()
        assert "start both streams" in window._calibrate_fov_status_label.text().lower()

    def test_status_message_when_no_plate_scale_config_is_available(self, qapp: object) -> None:
        guide = _starfield(200, 200, n_stars=40, seed=1)
        main_array = guide[60:140, 60:140].copy()
        window = MainWindow(
            ReplayCamera.from_arrays([main_array], cycle=True),
            guide_camera=ReplayCamera.from_arrays([guide], cycle=True),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=0.0,  # given-but-invalid — see FovOverlayIntegration
            guide_pixel_scale_arcsec=0.0,
        )
        window._left_panel._start_button.setChecked(True)
        window._right_panel._start_button.setChecked(True)
        try:
            window._left_panel._poll_frame()
            window._right_panel._poll_frame()
        finally:
            window._left_panel._start_button.setChecked(False)
            window._right_panel._start_button.setChecked(False)

        window._on_calibrate_fov()
        assert "plate-scale" in window._calibrate_fov_status_label.text().lower()

    def test_a_confident_match_sets_the_guide_panels_polygon(self, qapp: object) -> None:
        # Small enough that even the *default* (production) search
        # parameters — no test-only overrides — complete in ~1s.
        guide = _starfield(80, 80, n_stars=30, seed=2)
        main_array = guide[20:60, 15:65].copy()  # a real, matchable sub-crop
        window = MainWindow(
            ReplayCamera.from_arrays([main_array], cycle=True),
            guide_camera=ReplayCamera.from_arrays([guide], cycle=True),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=1.0,
            guide_pixel_scale_arcsec=1.0,
        )
        window._left_panel._start_button.setChecked(True)
        window._right_panel._start_button.setChecked(True)
        try:
            window._left_panel._poll_frame()
            window._right_panel._poll_frame()
        finally:
            window._left_panel._start_button.setChecked(False)
            window._right_panel._start_button.setChecked(False)

        assert window._left_panel.latest_mono_frame() is not None
        assert window._right_panel.latest_mono_frame() is not None

        window._on_calibrate_fov()
        assert not window._calibrate_fov_button.isEnabled()
        assert window._fov_calibrator.submit(  # a second submit while running is a no-op
            main_array, guide, approx_scale=1.0
        ) is False

        deadline = time.monotonic() + 15.0
        while window._calibrate_fov_poll_timer.isActive():
            assert time.monotonic() < deadline, "calibration never completed"
            time.sleep(0.02)
            window._poll_fov_calibration()

        assert window._calibrate_fov_button.isEnabled()
        assert window._right_panel._fov_polygon is not None
        assert len(window._right_panel._fov_polygon) == 4
        assert "calibrated" in window._calibrate_fov_status_label.text().lower()

    def test_status_shows_progress_while_calibration_is_running(self, qapp: object) -> None:
        """See the real bug: "Calibration started but working without any
        status on progress" — a static message for a ~2-minute search
        looked indistinguishable from a hang."""
        guide = _starfield(120, 120, n_stars=40, seed=3)
        main_array = guide[30:90, 25:100].copy()
        window = MainWindow(
            ReplayCamera.from_arrays([main_array], cycle=True),
            guide_camera=ReplayCamera.from_arrays([guide], cycle=True),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=1.0,
            guide_pixel_scale_arcsec=1.0,
        )
        window._left_panel._start_button.setChecked(True)
        window._right_panel._start_button.setChecked(True)
        try:
            window._left_panel._poll_frame()
            window._right_panel._poll_frame()
        finally:
            window._left_panel._start_button.setChecked(False)
            window._right_panel._start_button.setChecked(False)

        window._on_calibrate_fov()

        deadline = time.monotonic() + 15.0
        saw_progress_text = False
        while window._calibrate_fov_poll_timer.isActive():
            assert time.monotonic() < deadline, "calibration never completed"
            time.sleep(0.01)
            window._poll_fov_calibration()
            status = window._calibrate_fov_status_label.text()
            if "/" in status and "%" in status:
                saw_progress_text = True

        assert saw_progress_text, "status label never showed a completed/total progress update"

    def test_keep_calibrating_is_off_by_default_and_does_not_auto_restart(
        self, qapp: object
    ) -> None:
        guide = _starfield(80, 80, n_stars=30, seed=42)
        main_array = guide[20:60, 15:65].copy()
        window = MainWindow(
            ReplayCamera.from_arrays([main_array], cycle=True),
            guide_camera=ReplayCamera.from_arrays([guide], cycle=True),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=1.0,
            guide_pixel_scale_arcsec=1.0,
        )
        window._left_panel._start_button.setChecked(True)
        window._right_panel._start_button.setChecked(True)
        try:
            window._left_panel._poll_frame()
            window._right_panel._poll_frame()
        finally:
            window._left_panel._start_button.setChecked(False)
            window._right_panel._start_button.setChecked(False)

        assert not window._auto_recalibrate_checkbox.isChecked()
        window._on_calibrate_fov()

        deadline = time.monotonic() + 15.0
        while window._calibrate_fov_poll_timer.isActive():
            assert time.monotonic() < deadline, "calibration never completed"
            time.sleep(0.02)
            window._poll_fov_calibration()

        # One run completed and nothing restarted it.
        assert not window._calibrate_fov_poll_timer.isActive()
        assert not window._fov_calibrator.is_busy
        window.close()

    def test_keep_calibrating_checked_restarts_after_the_overlay_updates(
        self, qapp: object
    ) -> None:
        guide = _starfield(80, 80, n_stars=30, seed=5)
        main_array = guide[20:60, 15:65].copy()
        window = MainWindow(
            ReplayCamera.from_arrays([main_array], cycle=True),
            guide_camera=ReplayCamera.from_arrays([guide], cycle=True),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=1.0,
            guide_pixel_scale_arcsec=1.0,
        )
        window._left_panel._start_button.setChecked(True)
        window._right_panel._start_button.setChecked(True)
        try:
            window._left_panel._poll_frame()
            window._right_panel._poll_frame()
        finally:
            window._left_panel._start_button.setChecked(False)
            window._right_panel._start_button.setChecked(False)

        # Detect the auto-restart via a *decrease* in reported progress
        # (a fresh run's `completed` resets to a low number after the
        # previous run finished near its own total) rather than watching
        # the poll timer go idle or the status text change: with
        # auto-restart on, a completed run is immediately followed by a
        # new submit() inside the same _poll_fov_calibration() call, so
        # neither isActive() nor the status label are ever observed in
        # their momentary "just finished" state from outside. (An earlier
        # version of this test instead wrapped FovCalibrator.submit in a
        # counting closure — that captured the original *bound* method,
        # creating a reference cycle back through `__self__` to the very
        # object being patched, which delayed this whole window's
        # QTimers past normal refcounting cleanup into an unpredictable
        # later GC pass and crashed a later, unrelated test.)
        window._auto_recalibrate_checkbox.setChecked(True)
        window._on_calibrate_fov()

        restarted = False
        last_completed = 0
        deadline = time.monotonic() + 15.0
        while not restarted:
            assert time.monotonic() < deadline, "auto-restart was never observed"
            time.sleep(0.01)
            window._poll_fov_calibration()
            progress = window._fov_calibrator.latest_progress()
            if progress is not None:
                completed, _total = progress
                if completed < last_completed:
                    restarted = True
                last_completed = completed
        assert window._right_panel._fov_polygon is not None  # the first run's overlay landed

        # A second run is now in flight — turn the switch off so it
        # doesn't restart again, then let this one finish.
        window._auto_recalibrate_checkbox.setChecked(False)
        deadline = time.monotonic() + 15.0
        while window._calibrate_fov_poll_timer.isActive():
            assert time.monotonic() < deadline, "final calibration never completed"
            time.sleep(0.01)
            window._poll_fov_calibration()

        assert not window._calibrate_fov_poll_timer.isActive()

    def test_changing_a_connected_camera_clears_a_stale_calibrated_polygon(
        self, qapp: object
    ) -> None:
        window = MainWindow(
            _camera_with_sensor(200, 200),
            guide_camera=_camera_with_sensor(200, 200),
            device_lister=lambda: [],
            main_pixel_scale_arcsec=1.0,
            guide_pixel_scale_arcsec=1.0,
        )
        window._right_panel.set_fov_polygon([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
        window._update_fov_overlay()
        assert window._right_panel._fov_polygon is None


class TestCameraSettingsPersistence:
    """"Add storing own settings for cameras connected as the default
    startup settings. Assumption is, that the hardware will not change
    each time" — see astrotool_core.config.camera_settings. `tmp_path`
    isolation is automatic (see conftest.py's autouse fixture patching
    MainWindow's default settings path), so these tests don't need to
    pass camera_settings_path explicitly — two MainWindow(...) calls in
    the same test share the same tmp_path/config.toml, same as two real
    app launches would share the real config.toml."""

    def test_connecting_a_device_persists_its_camera_id(self, qapp: object) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M")]
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(),
        )
        window._left_panel._camera_combo.setCurrentIndex(1)
        window._left_panel._on_connect_camera()

        saved = load_camera_settings(window._camera_settings_path)
        assert saved["main"].camera_id == "dev-1"

    def test_editing_exposure_and_gain_persists_them(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        window._left_panel._exposure_spin.setValue(123.0)
        window._left_panel._gain_spin.setValue(150)

        saved = load_camera_settings(window._camera_settings_path)
        assert saved["main"].exposure_ms == pytest.approx(123.0)
        assert saved["main"].gain == 150

    def test_toggling_auto_exposure_persists_it(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        window._left_panel._auto_exposure_checkbox.setChecked(True)

        saved = load_camera_settings(window._camera_settings_path)
        assert saved["main"].auto_exposure_enabled is True

    def test_a_later_launch_restores_the_previous_camera_and_settings(
        self, qapp: object
    ) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M")]
        fake_touptek = FakeTouptekCamera()
        first = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: fake_touptek,
        )
        first._left_panel._camera_combo.setCurrentIndex(1)
        first._left_panel._on_connect_camera()
        # Auto-exposure toggled on *before* the manual exposure/gain edits:
        # checking it forces gain to the auto-exposure config's own default
        # (see _on_auto_exposure_toggled) — setting it last would clobber
        # the 200 this test means to persist and restore.
        first._left_panel._auto_exposure_checkbox.setChecked(True)
        first._left_panel._exposure_spin.setValue(321.0)
        first._left_panel._gain_spin.setValue(200)

        second = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: fake_touptek,
        )
        panel = second._left_panel
        assert panel._camera is fake_touptek
        assert panel._camera_combo.currentData() == devices[0]
        assert panel._exposure_spin.value() == pytest.approx(321.0)
        assert panel._gain_spin.value() == 200
        assert panel._auto_exposure_checkbox.isChecked()

    def test_restoring_a_custom_gain_survives_auto_exposure_also_being_restored_on(
        self, qapp: object
    ) -> None:
        """Checking "Auto exposure/gain" forces gain to the auto-exposure
        config's own default (see _on_auto_exposure_toggled) — restoring
        the checkbox after the saved gain would silently re-clobber it
        right back to that default."""
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        window._left_panel._auto_exposure_checkbox.setChecked(True)
        window._left_panel._gain_spin.setValue(777)

        second = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        assert second._left_panel._auto_exposure_checkbox.isChecked()
        assert second._left_panel._gain_spin.value() == 777

    def test_a_saved_camera_no_longer_enumerated_falls_back_to_the_demo_camera(
        self, qapp: object
    ) -> None:
        devices = [TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M")]
        first = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: devices,
            camera_factory=lambda camera_id: FakeTouptekCamera(),
        )
        first._left_panel._camera_combo.setCurrentIndex(1)
        first._left_panel._on_connect_camera()

        demo = _donut_camera((0.0, 0.0))
        second = MainWindow(demo, device_lister=lambda: [])  # dev-1 no longer present
        assert second._left_panel._camera is demo
        assert second._left_panel._camera_combo.currentData() is None

    def test_no_saved_settings_file_leaves_panels_on_the_demo_camera(
        self, qapp: object
    ) -> None:
        demo = _donut_camera((0.0, 0.0))
        window = MainWindow(demo, device_lister=lambda: [])
        assert window._left_panel._camera is demo
        assert not window._left_panel._auto_exposure_checkbox.isChecked()


class _ScriptedMovingFocuser(FakeFocuser):
    """FakeFocuser reports is_moving()==False always (moves complete
    instantly), which can't exercise a genuine Busy->Ok transition. This
    reports is_moving()==True for `busy_polls` simulated poll cycles
    after each move() before settling, so tests can observe FocuserPanel's
    _move_in_flight reacting to real Busy->Ok timing — see incident
    87349fd3. `is_moving()`/`status()` are plain, repeatable *reads* of
    the current busy state (like real cached hardware status) — call
    `tick()` once per simulated poll cycle to advance it, decoupled from
    however many times FocuserPanel itself happens to read is_moving()
    within one real _poll_status() call."""

    def __init__(self, *, busy_polls: int = 2) -> None:
        super().__init__()
        self._busy_polls = busy_polls
        self._remaining_busy = 0

    def move(self, steps: int) -> None:
        super().move(steps)
        self._remaining_busy = self._busy_polls

    def is_moving(self) -> bool:
        return self._remaining_busy > 0

    def status(self) -> FocuserStatus:
        # FakeFocuser.status() hardcodes moving=False regardless of
        # is_moving() (fine for its own always-instant-move purpose) --
        # FocuserPanel's busy-tracking reads status.moving, not
        # is_moving() directly, so this must actually delegate.
        base = super().status()
        return FocuserStatus(
            available=base.available,
            position=base.position,
            max_position=base.max_position,
            moving=self.is_moving(),
        )

    def tick(self) -> None:
        if self._remaining_busy > 0:
            self._remaining_busy -= 1


class TestFocuserPanel:
    """Manual in/out jog control for the main optical train's OnStep
    focuser — see FocuserPanel's docstring. FakeFocuser stands in for a
    real IndiFocuserAdapter here; the INDI wire protocol itself is
    covered by tests/core/indi and tests/core/focus instead."""

    def test_starts_disconnected_with_move_buttons_disabled(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=FakeFocuser()
        )
        panel = window._focuser_panel
        assert not panel._in_button.isEnabled()
        assert not panel._out_button.isEnabled()

    def test_connecting_enables_move_buttons(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=FakeFocuser()
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        assert panel._in_button.isEnabled()
        assert panel._out_button.isEnabled()

    def test_connect_failure_keeps_buttons_disabled_and_shows_the_error(
        self, qapp: object
    ) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
            focuser=FakeFocuser(fail_connect=True),
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        assert not panel._in_button.isEnabled()
        assert "failed" in panel._status_label.text().lower()

    def test_out_button_moves_outward_by_the_selected_step_size(self, qapp: object) -> None:
        focuser = FakeFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._step_group.button(5).setChecked(True)
        panel._out_button.click()
        assert focuser.get_position() == 5

    def test_in_button_moves_inward_by_the_selected_step_size(self, qapp: object) -> None:
        focuser = FakeFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._step_group.button(10).setChecked(True)
        panel._in_button.click()
        assert focuser.get_position() == -10

    def test_step_size_of_50_is_offered(self, qapp: object) -> None:
        focuser = FakeFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._step_group.button(50).setChecked(True)
        panel._out_button.click()
        assert focuser.get_position() == 50

    def test_default_step_size_is_one(self, qapp: object) -> None:
        focuser = FakeFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._out_button.click()
        assert focuser.get_position() == 1

    def test_status_label_shows_position_after_connecting(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=FakeFocuser()
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        assert "0" in panel._status_label.text()

    def test_disconnecting_disables_move_buttons_again(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=FakeFocuser()
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._connect_button.setChecked(False)
        assert not panel._in_button.isEnabled()

    def test_a_focuser_with_no_hardware_detected_keeps_buttons_disabled(
        self, qapp: object
    ) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
            focuser=FakeFocuser(available=False),
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        assert not panel._in_button.isEnabled()
        assert "no focuser hardware" in panel._status_label.text().lower()

    def test_diagnostic_context_includes_focuser_state(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=FakeFocuser()
        )
        window._focuser_panel._connect_button.setChecked(True)
        context = window._diagnostic_context()
        assert context["focuser"]["available"] is True

    def test_closing_the_window_disconnects_the_focuser(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=FakeFocuser()
        )
        window._focuser_panel._connect_button.setChecked(True)
        window.close()
        assert not window._focuser_panel._connected


class TestFocuserOneMoveAtATime:
    """Regression for incident 87349fd3: two relative moves issued to the
    real OnStep driver while the first is still in flight were found, on
    real hardware, to silently corrupt the result (only one of two 50-step
    moves actually landed, no error at all) -- see FocuserPanel's "One
    move at a time" docstring section."""

    def test_move_buttons_disable_immediately_on_click(self, qapp: object) -> None:
        focuser = _ScriptedMovingFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._out_button.click()
        assert not panel._in_button.isEnabled()
        assert not panel._out_button.isEnabled()

    def test_a_click_while_a_move_is_in_flight_is_ignored(self, qapp: object) -> None:
        focuser = _ScriptedMovingFocuser(busy_polls=5)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._out_button.click()
        assert focuser.get_position() == 1
        panel._out_button.click()  # disabled -- Qt refuses to fire clicked()
        assert focuser.get_position() == 1

    def test_buttons_reenable_after_a_genuine_busy_then_ok_transition(
        self, qapp: object
    ) -> None:
        focuser = _ScriptedMovingFocuser(busy_polls=2)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._out_button.click()
        assert not panel._out_button.isEnabled()

        # Genuinely busy for a couple of poll cycles first (the whole
        # point of this fixture) — not just re-enabled on the very next
        # poll regardless.
        panel._poll_status()
        assert not panel._out_button.isEnabled()
        focuser.tick()
        panel._poll_status()
        assert not panel._out_button.isEnabled()

        focuser.tick()  # settles: busy_polls exhausted
        panel._poll_status()
        assert panel._out_button.isEnabled()

    def test_a_stuck_busy_signal_is_released_by_the_confirmation_timeout(
        self, qapp: object
    ) -> None:
        """A focuser whose is_moving() never reports True at all (moves
        settle faster than this panel could ever observe) must not leave
        the buttons stuck disabled forever — see
        _MOVE_CONFIRMATION_TIMEOUT_S's docstring."""
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=FakeFocuser()
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._out_button.click()
        assert not panel._out_button.isEnabled()

        panel._move_issued_at = time.monotonic() - 999.0  # simulate elapsed time
        panel._poll_status()
        assert panel._out_button.isEnabled()


class _StuckMovingFocuser(FakeFocuser):
    """is_moving() reports False until the first move() (so a click can
    actually issue one at all, matching FocuserPanel's own is_moving()
    gate on the buttons), then True forever afterward, never settling on
    its own -- simulates real incident a4ffe048 (the driver never
    confirmed a Busy->Ok transition; position never updated either).
    Unlike _ScriptedMovingFocuser, nothing here ever resolves by itself
    -- these tests exist specifically to prove only Stop can recover."""

    def __init__(self) -> None:
        super().__init__()
        self.stop_log: list[None] = []
        self._move_issued = False

    def move(self, steps: int) -> None:
        super().move(steps)
        self._move_issued = True

    def is_moving(self) -> bool:
        return self._move_issued

    def status(self) -> FocuserStatus:
        base = super().status()
        return FocuserStatus(
            available=base.available,
            position=base.position,
            max_position=base.max_position,
            moving=self._move_issued,
        )

    def stop(self) -> None:
        self.stop_log.append(None)


class TestFocuserStop:
    """Regression for incident a4ffe048: "Focuser states moving, but
    position constant and no way to stop" -- In/Out could get gated
    disabled forever (real-hardware Busy that never confirms Ok, or this
    panel's own _move_in_flight tracking) with no escape hatch at all.
    FocuserPort.stop() already existed and sends the real hardware abort
    (FOCUS_ABORT_MOTION) -- it just wasn't wired to a button."""

    def test_stop_button_disabled_before_connecting(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=FakeFocuser()
        )
        assert not window._focuser_panel._stop_button.isEnabled()

    def test_stop_button_enabled_even_while_permanently_stuck_moving(
        self, qapp: object
    ) -> None:
        focuser = _StuckMovingFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._out_button.click()

        assert not panel._in_button.isEnabled()
        assert not panel._out_button.isEnabled()
        assert panel._stop_button.isEnabled()

    def test_stop_button_sends_the_real_hardware_abort(self, qapp: object) -> None:
        focuser = _StuckMovingFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._out_button.click()

        panel._stop_button.click()

        assert focuser.stop_log == [None]

    def test_stop_clears_this_panels_own_in_flight_tracking(self, qapp: object) -> None:
        focuser = _StuckMovingFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        panel = window._focuser_panel
        panel._connect_button.setChecked(True)
        panel._out_button.click()
        assert panel._move_in_flight is True

        panel._stop_button.click()

        assert panel._move_in_flight is False
        # In/Out stay disabled regardless -- the driver's own is_moving()
        # is still (permanently, for this double) reporting True, a real
        # hardware/firmware question Stop's own click can't paper over.
        assert not panel._in_button.isEnabled()

    def test_stop_on_a_disconnected_panel_is_a_safe_no_op(self, qapp: object) -> None:
        focuser = _StuckMovingFocuser()
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        window._focuser_panel._on_stop()
        assert focuser.stop_log == []


class TestQuittingStopsHardware:
    """Real report: "stop focuser and camera when quitting the APP. As
    well, stop the tracking on exit." closeEvent already called each
    panel's own stop(), but those only stopped polling/disconnected the
    INDI client socket -- none of them actually told the real hardware to
    stop moving first. See each panel's own stop()'s updated docstring.

    Constructs each panel directly (not via MainWindow) -- the changed
    logic lives entirely inside FocuserPanel.stop()/CameraPanel.stop()/
    MountParkPanel.stop() themselves, none of it depends on MainWindow's
    own cross-panel signal wiring, and a standalone panel is a much
    smaller Qt object graph. A version of this coverage built on a real
    MainWindow (even just one panel's worth) was found by bisection to
    measurably raise the odds of an existing, pre-existing-but-marginal
    Qt-teardown race (see conftest.py's `_flush_qt_events_after_each_test`
    docstring) actually reproducing under coverage instrumentation --
    every MainWindow-based variant tried still carried non-trivial risk,
    gone entirely once these were rewritten to skip MainWindow."""

    def test_stopping_the_focuser_panel_aborts_in_flight_motion(self, qapp: object) -> None:
        focuser = _StuckMovingFocuser()
        panel = FocuserPanel(focuser)
        panel._connect_button.setChecked(True)
        panel._out_button.click()
        assert focuser.stop_log == []

        panel.stop()

        assert focuser.stop_log == [None]

    def test_stopping_the_camera_panel_disconnects_the_camera(self, qapp: object) -> None:
        camera = _donut_camera((0.0, 0.0))
        disconnects: list[None] = []
        camera.disconnect = lambda: disconnects.append(None)  # type: ignore[method-assign]
        panel = CameraPanel(camera, title="Main", device_lister=lambda: [])

        panel.stop()

        assert disconnects == [None]

    def test_stopping_the_mount_panel_stops_tracking_without_parking(self, qapp: object) -> None:
        mount_park = FakeMountPark(start_parked=False)
        mount_park._tracking = True  # noqa: SLF001 -- simulate tracking left on
        panel = MountParkPanel(mount_park)
        panel._connect_button.setChecked(True)

        panel.stop()

        assert mount_park.stop_tracking_count == 1  # tracking stopped...
        assert mount_park.status().tracking is False
        assert mount_park.park_count == 0  # ...but deliberately not parked
        # "safe no-op when never connected" is covered at the MountParkPort
        # level instead (tests/contracts/test_mount_park_contract.py's
        # test_stop_tracking_is_safe_to_call_before_connect) -- no need for
        # a second panel here just to re-prove the same thing.


class TestFocuserPausesMainCamera:
    """The focuser sits on the main optical train only — MainWindow wires
    FocuserPanel.move_in_flight_changed to _left_panel.set_updates_paused
    (see both modules' docstrings) so live analysis/display never runs on
    a frame captured mid-jog. _right_panel (Guide) has no focuser and must
    be unaffected."""

    def test_a_jog_pauses_the_main_panel_but_not_the_guide_panel(self, qapp: object) -> None:
        focuser = _ScriptedMovingFocuser(busy_polls=5)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        window._focuser_panel._connect_button.setChecked(True)
        assert not window._left_panel._updates_paused
        window._focuser_panel._out_button.click()
        assert window._left_panel._updates_paused
        assert not window._right_panel._updates_paused

    def test_resumes_once_the_move_settles(self, qapp: object) -> None:
        focuser = _ScriptedMovingFocuser(busy_polls=2)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        window._focuser_panel._connect_button.setChecked(True)
        window._focuser_panel._out_button.click()
        assert window._left_panel._updates_paused

        focuser.tick()
        window._focuser_panel._poll_status()
        assert window._left_panel._updates_paused  # still busy_polls=2 -> 1 remaining

        focuser.tick()  # settles
        window._focuser_panel._poll_status()
        assert not window._left_panel._updates_paused

    def test_pausing_stops_the_main_panels_poll_timer_while_streaming(self, qapp: object) -> None:
        focuser = _ScriptedMovingFocuser(busy_polls=5)
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], focuser=focuser
        )
        window._focuser_panel._connect_button.setChecked(True)
        window._left_panel._start_button.setChecked(True)
        try:
            window._focuser_panel._out_button.click()
            assert not window._left_panel._timer.isActive()
            assert "focuser moving" in window._left_panel._recommendation_label.text().lower()
        finally:
            window._left_panel._start_button.setChecked(False)


class TestCameraPanelSetUpdatesPaused:
    """CameraPanel.set_updates_paused() directly — the guard behavior
    TestFocuserPausesMainCamera doesn't need a real focuser to exercise."""

    def test_pausing_while_not_streaming_leaves_the_timer_stopped(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel.set_updates_paused(True)
        assert panel._updates_paused
        assert not panel._timer.isActive()

    def test_resuming_does_not_start_the_timer_when_not_streaming(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel.set_updates_paused(True)
        panel.set_updates_paused(False)
        assert not panel._updates_paused
        assert not panel._timer.isActive()

    def test_starting_the_stream_while_paused_does_not_start_the_timer(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel.set_updates_paused(True)
        panel._start_button.setChecked(True)
        try:
            assert not panel._timer.isActive()
        finally:
            panel._start_button.setChecked(False)

    def test_resuming_while_streaming_restarts_the_timer(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._start_button.setChecked(True)
        try:
            panel.set_updates_paused(True)
            assert not panel._timer.isActive()
            panel.set_updates_paused(False)
            assert panel._timer.isActive()
        finally:
            panel._start_button.setChecked(False)

    def test_pausing_twice_is_a_no_op(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._start_button.setChecked(True)
        try:
            panel.set_updates_paused(True)
            panel._recommendation_label.setText("sentinel")
            panel.set_updates_paused(True)  # no-op -- must not touch the label again
            assert panel._recommendation_label.text() == "sentinel"
        finally:
            panel._start_button.setChecked(False)

    def test_diagnostic_context_reports_paused_state(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        window._left_panel.set_updates_paused(True)
        assert window._diagnostic_context()["left"]["updates_paused"] is True


class TestCameraPanelSetAutoExposurePaused:
    """CameraPanel.set_auto_exposure_paused() -- deliberately distinct from
    set_updates_paused (see both docstrings, and real incident ca728d27):
    frame capture/analysis must keep running, only the auto-exposure
    adjustment itself is suppressed."""

    def test_paused_frame_capture_still_runs_but_auto_exposure_does_not(
        self, qapp: object
    ) -> None:
        # Mirrors TestAutoExposure's own dim-donut case (same fixture,
        # same reasoning: the demo donut's peak is well below the target
        # band, so exposure would rise if auto-exposure ran at all).
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._exposure_spin.setValue(10.0)
        initial_exposure = panel._exposure_spin.value()
        panel._auto_exposure_checkbox.setChecked(True)
        panel._start_button.setChecked(True)
        try:
            panel.set_auto_exposure_paused(True)
            panel._poll_frame()  # frame capture keeps running while paused
            time.sleep(0.05)
            panel._poll_frame()
            assert len(panel._recent_frames) > 0
            assert panel._exposure_spin.value() == initial_exposure  # auto-exposure did not fire
        finally:
            panel._start_button.setChecked(False)

    def test_resuming_lets_auto_exposure_run_again(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._auto_exposure_checkbox.setChecked(True)
        panel.set_auto_exposure_paused(True)
        panel.set_auto_exposure_paused(False)
        assert panel._auto_exposure_paused is False

    def test_diagnostic_context_reports_auto_exposure_paused_state(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        window._left_panel.set_auto_exposure_paused(True)
        assert window._diagnostic_context()["left"]["auto_exposure_paused"] is True

    def test_current_exposure_gain_reports_the_live_spinbox_values(self, qapp: object) -> None:
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [])
        panel = window._left_panel
        panel._exposure_spin.setValue(12.5)
        panel._gain_spin.setValue(250)
        assert panel.current_exposure_gain() == (12.5, 250)


class _ScriptedTransitioningMountPark(FakeMountPark):
    """FakeMountPark's park()/unpark() settle instantly, which can't
    exercise a genuine Busy->Ok transition. This reports the *opposite*
    of the requested parked state for `busy_polls` simulated poll cycles
    after each park()/unpark() call before settling — mirroring
    _ScriptedMovingFocuser's role for the focuser panel. `tick()` once
    per simulated poll cycle, decoupled from however many times
    MountParkPanel itself happens to read status() within one real
    _poll_status() call."""

    def __init__(self, *, busy_polls: int = 2, start_parked: bool = True) -> None:
        super().__init__(start_parked=start_parked)
        self._busy_polls = busy_polls
        self._remaining_busy = 0
        self._pending_parked: bool | None = None

    def park(self) -> None:
        self._pending_parked = True
        self._remaining_busy = self._busy_polls

    def unpark(self) -> None:
        self._pending_parked = False
        self._remaining_busy = self._busy_polls
        self._tracking = False

    def status(self) -> MountParkStatus:
        base = super().status()
        if self._remaining_busy > 0 and self._pending_parked is not None:
            return MountParkStatus(
                available=base.available, parked=not self._pending_parked, tracking=base.tracking
            )
        return base

    def tick(self) -> None:
        if self._remaining_busy > 0:
            self._remaining_busy -= 1
            if self._remaining_busy == 0 and self._pending_parked is not None:
                self._parked = self._pending_parked
                self._pending_parked = None


class TestMountParkPanel:
    """Park/unpark-only control for the OnStep mount — see
    MountParkPanel's docstring. FakeMountPark stands in for a real
    IndiMountParkAdapter here; the INDI wire protocol itself is covered
    by tests/core/mount and tests/contracts instead."""

    def test_starts_disconnected_with_action_buttons_disabled(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], mount=FakeMountPark()
        )
        panel = window._mount_panel
        assert not panel._park_button.isEnabled()
        assert not panel._unpark_button.isEnabled()

    def test_connecting_while_parked_enables_only_unpark(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
            mount=FakeMountPark(start_parked=True),
        )
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        assert not panel._park_button.isEnabled()
        assert panel._unpark_button.isEnabled()
        window.close()  # stop the panel's poll timer — see conftest.py's Qt-flush fixture

    def test_connecting_while_unparked_enables_only_park(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
            mount=FakeMountPark(start_parked=False),
        )
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        assert panel._park_button.isEnabled()
        assert not panel._unpark_button.isEnabled()
        window.close()

    def test_connect_failure_keeps_buttons_disabled_and_shows_the_error(
        self, qapp: object
    ) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
            mount=FakeMountPark(fail_connect=True),
        )
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        assert not panel._park_button.isEnabled()
        assert not panel._unpark_button.isEnabled()
        assert "failed" in panel._status_label.text().lower()

    def test_unpark_clears_parked_state_and_deactivates_tracking(self, qapp: object) -> None:
        mount = FakeMountPark(start_parked=True)
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [], mount=mount)
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        mount._tracking = True  # simulate a prior session left tracking on  # noqa: SLF001
        panel._unpark_button.click()
        assert mount.status().parked is False
        assert mount.status().tracking is False
        window.close()

    def test_park_sets_parked_state(self, qapp: object) -> None:
        mount = FakeMountPark(start_parked=False)
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [], mount=mount)
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        panel._park_button.click()
        assert mount.status().parked is True
        window.close()

    def test_a_mount_with_no_interface_detected_keeps_buttons_disabled(
        self, qapp: object
    ) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
            mount=FakeMountPark(available=False),
        )
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        assert not panel._park_button.isEnabled()
        assert not panel._unpark_button.isEnabled()
        assert "no mount interface" in panel._status_label.text().lower()
        window.close()

    def test_diagnostic_context_includes_mount_state(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], mount=FakeMountPark()
        )
        window._mount_panel._connect_button.setChecked(True)
        context = window._diagnostic_context()
        assert context["mount"]["available"] is True
        window.close()

    def test_closing_the_window_disconnects_the_mount(self, qapp: object) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)), device_lister=lambda: [], mount=FakeMountPark()
        )
        window._mount_panel._connect_button.setChecked(True)
        window.close()
        assert not window._mount_panel._connected


class TestMountParkOneActionAtATime:
    """Same class of protection as TestFocuserOneMoveAtATime, for the
    same reason: park/unpark is a slow, real hardware transition, so a
    second click must not be able to race the first."""

    def test_action_buttons_disable_immediately_on_click(self, qapp: object) -> None:
        mount = _ScriptedTransitioningMountPark(start_parked=True)
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [], mount=mount)
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        panel._unpark_button.click()
        assert not panel._park_button.isEnabled()
        assert not panel._unpark_button.isEnabled()
        window.close()

    def test_a_click_while_an_action_is_in_flight_is_ignored(self, qapp: object) -> None:
        mount = _ScriptedTransitioningMountPark(start_parked=True, busy_polls=5)
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [], mount=mount)
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        panel._unpark_button.click()
        panel._park_button.click()  # disabled -- Qt refuses to fire clicked()
        mount.tick()
        mount.tick()
        mount.tick()
        mount.tick()
        mount.tick()
        panel._poll_status()
        assert mount.status().parked is False  # the ignored park() never took effect
        window.close()

    def test_buttons_reenable_after_a_genuine_busy_then_ok_transition(
        self, qapp: object
    ) -> None:
        mount = _ScriptedTransitioningMountPark(start_parked=True, busy_polls=2)
        window = MainWindow(_donut_camera((0.0, 0.0)), device_lister=lambda: [], mount=mount)
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        panel._unpark_button.click()
        assert not panel._unpark_button.isEnabled()

        panel._poll_status()
        assert not panel._park_button.isEnabled()
        mount.tick()
        panel._poll_status()
        assert not panel._park_button.isEnabled()

        mount.tick()  # settles: busy_polls exhausted
        panel._poll_status()
        assert panel._park_button.isEnabled()
        assert not panel._unpark_button.isEnabled()
        window.close()

    def test_a_stuck_busy_signal_is_released_by_the_confirmation_timeout(
        self, qapp: object
    ) -> None:
        window = MainWindow(
            _donut_camera((0.0, 0.0)),
            device_lister=lambda: [],
            mount=FakeMountPark(start_parked=True),
        )
        panel = window._mount_panel
        panel._connect_button.setChecked(True)
        panel._unpark_button.click()

        panel._action_issued_at = time.monotonic() - 999.0  # simulate elapsed time
        panel._poll_status()
        assert panel._park_button.isEnabled()
        window.close()


class _SlowMountPark(FakeMountPark):
    """FakeMountPark whose unpark() takes a moment to settle -- a plain
    FakeMountPark/FakeMountAdapter pair settle instantly, so
    MountTestMoveRunner's background thread can finish before a test's
    own next line runs; this keeps it observably busy for a beat."""

    def unpark(self) -> None:
        time.sleep(0.3)
        super().unpark()


class TestMountTestMovePanel:
    """Mount-alignment tool — see MountTestMovePanel's docstring.
    FakeMountAdapter stands in for a real IndiMountPulseAdapter here; the
    INDI wire protocol itself is covered by tests/core/mount and
    tests/contracts instead. The math (compose_screen_move) is covered by
    tests/core/mount/test_axis_calibration.py; MountTestMoveRunner's own
    sequencing is covered by test_mount_test_move_runner.py. These tests
    are about the panel's own wiring (connect lifecycle, calibration
    driving the runner in sequence, per-camera nudge gating, result
    rendering) -- deliberately using static single-frame cameras (like
    every other class in this file), so every calibrated AxisResponse is
    (dx=0, dy=0): fine for star mode (a real response either way, see
    response_from_positions), degenerate for a nudge's compose_screen_move
    (AXIS1/AXIS2 measure literally identical zero vectors) -- the "click a
    nudge and see it submit the right pulses" test below injects its own
    non-degenerate CalibrationMatrix directly rather than fighting a real
    streaming camera's own timing to script two different frames at two
    different moments (see ReplayCamera/StreamController: frames arrive on
    a background thread, not one-per-_poll_frame()-call)."""

    def _window(self, *, mount_park: FakeMountPark, pulse_mount: FakeMountAdapter) -> MainWindow:
        return MainWindow(
            _star_camera(50.0, 50.0),
            guide_camera=_star_camera(20.0, 30.0),
            device_lister=lambda: [],
            mount=mount_park,
            pulse_mount=pulse_mount,
        )

    def _connect_and_stream_cameras(self, window: MainWindow) -> None:
        window._left_panel._start_button.setChecked(True)
        window._right_panel._start_button.setChecked(True)
        window._left_panel._poll_frame()
        window._right_panel._poll_frame()

    def _run_calibration_to_completion(self, panel: object, *, timeout_s: float = 15.0) -> None:
        """Click Run Calibration and drive the panel's poll loop until the
        whole 4-step sequence finishes (or the timeout fires) -- mirrors
        the existing "while runner.is_busy: sleep; poll()" pattern used
        throughout this file for a single pulse, just repeated across the
        sequence's several pulses.

        `timeout_s` default raised from 5.0: MountAlignmentSettings'
        default settle_ms (1000ms, real -- see that module's own
        docstring) is a genuine per-step delay in MountTestMoveRunner
        itself, not something a fake mount/park speeds up -- 4 real
        calibration steps alone already cost ~4s before any other
        overhead (frame capture, a slow mount_park fake, etc.)."""
        panel._run_calibration_button.click()  # type: ignore[attr-defined]
        deadline = time.monotonic() + timeout_s
        while panel._calibration_queue or panel._pending is not None:  # type: ignore[attr-defined]
            while panel._runner.is_busy:  # type: ignore[attr-defined]
                assert time.monotonic() < deadline, "calibration never completed"
                time.sleep(0.01)
            panel._poll()  # type: ignore[attr-defined]

    def test_starts_disconnected_with_calibration_and_nudge_buttons_disabled(
        self, qapp: object
    ) -> None:
        window = self._window(mount_park=FakeMountPark(), pulse_mount=FakeMountAdapter())
        panel = window._test_move_panel
        assert not panel._run_calibration_button.isEnabled()
        assert all(
            not button.isEnabled()
            for pad in panel._nudge_buttons.values()
            for button in pad.values()
        )

    def test_calibration_button_enabled_once_connected_regardless_of_park_state(
        self, qapp: object
    ) -> None:
        # Unlike the old raw N/S/E/W buttons, calibration no longer needs
        # the mount already parked/unparked -- MountTestMoveRunner handles
        # unparking transparently for every pulse it issues.
        for start_parked in (True, False):
            window = self._window(
                mount_park=FakeMountPark(start_parked=start_parked), pulse_mount=FakeMountAdapter()
            )
            window._mount_panel._connect_button.setChecked(True)
            window._test_move_panel._connect_button.setChecked(True)
            assert window._test_move_panel._run_calibration_button.isEnabled()
            window.close()

    def test_connect_failure_keeps_calibration_button_disabled_and_shows_the_error(
        self, qapp: object
    ) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=True),
            pulse_mount=FakeMountAdapter(fail_connect=True),
        )
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel
        assert not panel._run_calibration_button.isEnabled()
        assert "failed" in panel._status_label.text().lower()
        # mount_park's own connect succeeded (only pulse_mount fails) --
        # its poll timer is running and must be stopped, see conftest.py's
        # Qt-flush fixture / the segfault class this class of leak causes.
        window.close()

    def test_run_calibration_pulses_all_four_steps_in_order_and_builds_both_matrices(
        self, qapp: object
    ) -> None:
        pulse_mount = FakeMountAdapter()
        mount_park = FakeMountPark(start_parked=True)
        window = self._window(mount_park=mount_park, pulse_mount=pulse_mount)
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        # A real, non-degenerate calibration needs AXIS1's and AXIS2's own
        # measured responses to actually differ -- this file's shared
        # _star_camera fixture is a single static frame (same star, same
        # position, every capture), which would make every axis response
        # exactly (0, 0) and silently exercise the *degenerate* case
        # instead (see is_degenerate() and the new
        # test_run_calibration_reports_a_degenerate_axis... below). Fake
        # real per-axis motion by returning a star at a different position
        # each capture -- same monkeypatch pattern as flaky_get_left_frame
        # a few tests below. Capture order per camera: axis1 before,
        # axis1 after, axis2 before, axis2 after (the return steps take no
        # measurement).
        left_positions = iter([(50.0, 50.0), (60.0, 50.0), (50.0, 50.0), (50.0, 60.0)])
        right_positions = iter([(50.0, 50.0), (60.0, 50.0), (50.0, 50.0), (50.0, 60.0)])

        def stepped_left_frame() -> np.ndarray:
            x, y = next(left_positions)
            return single_star_image((120, 120), x=x, y=y, peak=2000.0, sigma=2.5, background=100.0)

        def stepped_right_frame() -> np.ndarray:
            x, y = next(right_positions)
            return single_star_image((120, 120), x=x, y=y, peak=2000.0, sigma=2.5, background=100.0)

        panel._get_left_frame = stepped_left_frame
        panel._get_right_frame = stepped_right_frame

        self._run_calibration_to_completion(panel)

        settings = MountAlignmentSettings()
        assert pulse_mount.pulse_log == [
            (MountAxis.AXIS1, AxisDirection.POSITIVE, settings.pulse_ms),
            (MountAxis.AXIS1, AxisDirection.NEGATIVE, settings.pulse_ms),
            (MountAxis.AXIS2, AxisDirection.POSITIVE, settings.pulse_ms),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE, settings.pulse_ms),
        ]
        assert pulse_mount.rate_log == [settings.rate_preset] * 4
        assert set(panel._calibration) == {"left", "right"}
        assert all(
            button.isEnabled()
            for pad in panel._nudge_buttons.values()
            for button in pad.values()
        )
        assert "RA-axis" in panel._result_label.text()
        assert "Dec-axis" in panel._result_label.text()
        window.close()

    def test_calibration_steps_pass_the_configured_settle_ms_to_the_runner(
        self, qapp: object
    ) -> None:
        """Real report: "calibration doesn't wait for mount to be
        stabilized" -- confirms the configured settle_ms actually reaches
        MountTestMoveRunner.submit() for every one of Run Calibration's
        four steps, not just that the setting exists."""
        pulse_mount = FakeMountAdapter()
        mount_park = FakeMountPark(start_parked=True)
        window = self._window(mount_park=mount_park, pulse_mount=pulse_mount)
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        settle_values: list[int | None] = []
        real_submit = panel._runner.submit

        def spy_submit(
            mount_park: MountParkPort,
            mount: MountPort,
            axis: MountAxis,
            direction: AxisDirection,
            pulse_ms: int,
            *,
            rate_preset: str | None = None,
            park_after: bool = True,
            settle_ms: int = 0,
        ) -> bool:
            settle_values.append(settle_ms)
            return real_submit(
                mount_park, mount, axis, direction, pulse_ms,
                rate_preset=rate_preset, park_after=park_after, settle_ms=settle_ms,
            )

        panel._runner.submit = spy_submit  # type: ignore[method-assign]

        self._run_calibration_to_completion(panel)

        assert settle_values == [MountAlignmentSettings().settle_ms] * 4
        window.close()

    def test_calibration_pauses_both_cameras_auto_exposure_and_resumes_after(
        self, qapp: object
    ) -> None:
        """Real incident ca728d27: live auto-exposure roughly doubled a
        camera's gain between a step's before/after capture, corrupting
        the measured displacement. MainWindow wires MountTestMovePanel's
        pause/resume to CameraPanel.set_auto_exposure_paused (not
        set_updates_paused -- frame capture must keep running)."""
        pulse_mount = FakeMountAdapter()
        mount_park = FakeMountPark(start_parked=True)
        window = self._window(mount_park=mount_park, pulse_mount=pulse_mount)
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        assert window._left_panel._auto_exposure_paused is False
        assert window._right_panel._auto_exposure_paused is False

        panel._run_calibration_button.click()
        assert window._left_panel._auto_exposure_paused is True
        assert window._right_panel._auto_exposure_paused is True

        self._run_calibration_to_completion(panel)

        assert window._left_panel._auto_exposure_paused is False
        assert window._right_panel._auto_exposure_paused is False
        window.close()

    def test_run_calibration_never_reparks_the_mount(self, qapp: object) -> None:
        mount_park = FakeMountPark(start_parked=True)
        window = self._window(mount_park=mount_park, pulse_mount=FakeMountAdapter())
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        self._run_calibration_to_completion(panel)

        assert mount_park.park_count == 0
        assert mount_park.status().parked is False
        window.close()

    def test_a_measurement_failure_after_a_successful_pulse_still_returns_that_axis(
        self, qapp: object
    ) -> None:
        """Regression test for diagnostic a082144a: "with failed
        calibration not returning to start point". AXIS2's forward pulse
        succeeds, but its own "after" measurement fails on the 4th frame
        capture (simulated by having the left camera's frame getter go
        missing exactly then) -- the old _abort_calibration cleared the
        whole remaining queue unconditionally, silently dropping AXIS2's
        own already-queued return pulse and leaving the mount stranded
        off its start position. It must still be sent."""
        pulse_mount = FakeMountAdapter()
        mount_park = FakeMountPark(start_parked=True)
        window = self._window(mount_park=mount_park, pulse_mount=pulse_mount)
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        # Capture order is: AXIS1 before, AXIS1 after, [AXIS1 return has no
        # capture], AXIS2 before, AXIS2 after -- the 4th left-camera call.
        # Fail exactly that one so AXIS1 completes normally and AXIS2's
        # forward pulse has already been sent before anything fails.
        real_get_left_frame = panel._get_left_frame
        call_count = 0

        def flaky_get_left_frame() -> np.ndarray | None:
            nonlocal call_count
            call_count += 1
            return None if call_count >= 4 else real_get_left_frame()

        panel._get_left_frame = flaky_get_left_frame

        self._run_calibration_to_completion(panel)
        # Real report: "calibration failed is stated already while mount
        # is moving" -- the message shown the instant the queue/pending
        # state above clears must say the mount is still returning, not
        # read as final, since the stranded return pulse is still
        # physically in flight at this exact point.
        assert "Calibration failed" in panel._result_label.text()
        assert "returning mount to start position" in panel._result_label.text()

        # The stranded-return pulse is fire-and-forget (no _pending to
        # track), so _run_calibration_to_completion's queue/pending-based
        # wait can return before it actually finishes -- wait for the
        # runner directly too, then poll once more to pick up its outcome.
        deadline = time.monotonic() + 5.0
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "stranded return pulse never completed"
            time.sleep(0.01)
        panel._poll()

        settings = MountAlignmentSettings()
        assert pulse_mount.pulse_log == [
            (MountAxis.AXIS1, AxisDirection.POSITIVE, settings.pulse_ms),
            (MountAxis.AXIS1, AxisDirection.NEGATIVE, settings.pulse_ms),
            (MountAxis.AXIS2, AxisDirection.POSITIVE, settings.pulse_ms),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE, settings.pulse_ms),
        ]
        # Now settled -- the message reflects the return pulse's own
        # actual, now-known outcome instead of still saying "returning".
        assert "Calibration failed" in panel._result_label.text()
        assert "mount returned to start position" in panel._result_label.text()
        assert "returning mount to start position" not in panel._result_label.text()
        window.close()

    def test_stranded_return_pulse_failure_is_reported_as_a_warning(
        self, qapp: object
    ) -> None:
        """Same setup as the test above, but the stranded return pulse
        itself is rejected -- must not claim the mount is back home when
        it isn't; a real pulse can fail (see MountTestMoveRunner's own
        retry logic, which covers transient rejection but not every
        possible failure)."""

        class _RejectsAxis2NegativeMount(FakeMountAdapter):
            """Every pulse succeeds normally except AXIS2 NEGATIVE (the
            one this test needs to be the stranded return step) --
            targeted by (axis, direction) rather than attempt count, so
            AXIS1's own test+return and AXIS2's forward pulse (which must
            all succeed for this scenario to even reach AXIS2's stranded
            return) are unaffected."""

            def pulse_axis(
                self,
                axis: MountAxis,
                direction: AxisDirection,
                duration_ms: int,
                *,
                rate_preset: str | None = None,
            ) -> CommandResult:
                if axis is MountAxis.AXIS2 and direction is AxisDirection.NEGATIVE:
                    return CommandResult(accepted=False, message="simulated rejection")
                return super().pulse_axis(axis, direction, duration_ms, rate_preset=rate_preset)

        pulse_mount = _RejectsAxis2NegativeMount()
        mount_park = FakeMountPark(start_parked=True)
        window = self._window(mount_park=mount_park, pulse_mount=pulse_mount)
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel
        import collimation_tool.ui.mount_test_move_runner as runner_module

        original_delay = runner_module._PULSE_REJECTION_RETRY_DELAY_S
        runner_module._PULSE_REJECTION_RETRY_DELAY_S = 0.01  # keep the test fast

        real_get_left_frame = panel._get_left_frame
        call_count = 0

        def flaky_get_left_frame() -> np.ndarray | None:
            nonlocal call_count
            call_count += 1
            return None if call_count >= 4 else real_get_left_frame()

        panel._get_left_frame = flaky_get_left_frame

        try:
            self._run_calibration_to_completion(panel)
            deadline = time.monotonic() + 5.0
            while panel._runner.is_busy:
                assert time.monotonic() < deadline, "stranded return pulse never completed"
                time.sleep(0.01)
            panel._poll()
        finally:
            runner_module._PULSE_REJECTION_RETRY_DELAY_S = original_delay

        assert "Calibration failed" in panel._result_label.text()
        assert "WARNING: mount may not have returned to start position" in (
            panel._result_label.text()
        )
        window.close()

    def test_run_calibration_button_and_nudge_pads_disable_and_stop_enables_mid_sequence(
        self, qapp: object
    ) -> None:
        # A plain FakeMountPark/FakeMountAdapter settle instantly, so the
        # runner's background thread can finish before this test's own
        # next line runs -- _SlowMountPark keeps it busy long enough to
        # actually observe the in-flight button states.
        window = self._window(
            mount_park=_SlowMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel
        assert not panel._stop_button.isEnabled()

        panel._run_calibration_button.click()
        # Button state updates synchronously, right after submit() -- no
        # poll tick needed to see this.
        assert panel._stop_button.isEnabled()
        assert not panel._run_calibration_button.isEnabled()

        self._run_calibration_to_completion(panel)
        assert not panel._stop_button.isEnabled()
        window.close()

    def test_stop_calls_abort_on_the_pulse_mount(self, qapp: object) -> None:
        pulse_mount = FakeMountAdapter()
        window = self._window(mount_park=FakeMountPark(start_parked=True), pulse_mount=pulse_mount)
        window._test_move_panel._connect_button.setChecked(True)
        window._test_move_panel._on_stop()
        assert pulse_mount.abort_log == [None]
        window.close()

    def test_diagnostic_context_includes_calibration_and_last_result(self, qapp: object) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        self._run_calibration_to_completion(panel)

        context = window._diagnostic_context()
        calibration = context["mount_test_move"]["calibration"]
        assert set(calibration) == {"left", "right"}
        assert set(calibration["left"]) == {"axis1", "axis2"}
        assert set(calibration["left"]["axis1"]) == {
            "dx_px", "dy_px", "magnitude_px", "angle_degrees",
        }
        assert context["mount_test_move"]["last_result"] is not None
        assert set(context["mount_test_move"]["last_result"]) == {"left", "right"}
        window.close()

    def test_diagnostic_frames_capture_the_actual_before_after_pairs_used(
        self, qapp: object
    ) -> None:
        """Regression coverage for diagnostic de271da5: a pulled bundle's
        frames used to be "whatever's currently streaming" (each camera
        panel's own recent-frames ring buffer), not necessarily what a
        calibration step's own measurement actually used. Every step's
        raw before/after pair must be individually retrievable, labelled
        by axis and before/after, for both cameras."""
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        self._run_calibration_to_completion(panel)

        frames = panel.diagnostic_frames()
        assert set(frames) == {
            "axis1_before_left", "axis1_before_right",
            "axis1_after_left", "axis1_after_right",
            "axis2_before_left", "axis2_before_right",
            "axis2_after_left", "axis2_after_right",
        }
        assert all(isinstance(array, np.ndarray) for array in frames.values())
        window.close()

    def test_main_window_folds_calibration_diagnostic_frames_into_the_saved_bundle(
        self, qapp: object
    ) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        self._run_calibration_to_completion(panel)

        calib_sources = set()
        for frame in window._all_recent_frames():
            if isinstance(frame.header, fits.Header) and "CALIBSRC" in frame.header:
                calib_sources.add(frame.header["CALIBSRC"])
        assert "axis1_before_left" in calib_sources
        assert "axis2_after_right" in calib_sources
        window.close()

    def test_calibration_diagnostic_frames_carry_the_exposure_and_gain_used(
        self, qapp: object
    ) -> None:
        """Regression coverage for incidents ca728d27/0de26787: whether
        auto-exposure changed gain *between* a step's before/after
        capture kept being the open question a diagnostic bundle
        couldn't actually answer -- the saved pixels alone don't carry
        the camera settings they were taken with. Every CALIBSRC frame
        must now also carry EXPOSURE (seconds)/GAIN."""
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        self._run_calibration_to_completion(panel)

        checked = 0
        for frame in window._all_recent_frames():
            if isinstance(frame.header, fits.Header) and "CALIBSRC" in frame.header:
                assert "EXPOSURE" in frame.header
                assert "GAIN" in frame.header
                assert frame.header["EXPOSURE"] > 0.0
                assert frame.header["GAIN"] > 0
                checked += 1
        assert checked == 8  # every CALIBSRC frame, not just some
        window.close()

    def test_closing_the_window_disconnects_the_pulse_mount(self, qapp: object) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        window._test_move_panel._connect_button.setChecked(True)
        window.close()
        assert not window._test_move_panel._connected

    def test_target_defaults_to_star(self, qapp: object) -> None:
        window = self._window(mount_park=FakeMountPark(), pulse_mount=FakeMountAdapter())
        panel = window._test_move_panel
        assert panel._star_button.isChecked()
        assert not panel._terrestrial_button.isChecked()
        assert panel._target_mode() == "star"

    def test_selecting_terrestrial_switches_the_mode(self, qapp: object) -> None:
        window = self._window(mount_park=FakeMountPark(), pulse_mount=FakeMountAdapter())
        panel = window._test_move_panel
        panel._terrestrial_button.click()
        assert panel._target_mode() == "terrestrial"
        assert not panel._star_button.isChecked()

    def test_star_mode_calibration_aborts_immediately_on_a_textureless_camera(
        self, qapp: object
    ) -> None:
        """Real incident 6fa2aa59: a daytime/indoor capture with no star
        correctly refuses rather than moving the real mount for nothing —
        this is the failure Terrestrial mode exists to work around."""
        pulse_mount = FakeMountAdapter()
        window = MainWindow(
            _textured_camera(seed=10),
            guide_camera=_textured_camera(seed=11),
            device_lister=lambda: [],
            mount=FakeMountPark(start_parked=True),
            pulse_mount=pulse_mount,
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        panel._run_calibration_button.click()

        assert "no star detected" in panel._result_label.text()
        assert not panel._runner.is_busy  # never submitted -- no pulse issued
        assert pulse_mount.pulse_log == []
        assert not panel._calibration
        window.close()

    def test_terrestrial_mode_calibration_succeeds_on_a_textureless_camera_via_cross_correlation(
        self, qapp: object
    ) -> None:
        pulse_mount = FakeMountAdapter()
        window = MainWindow(
            _textured_camera(seed=10),
            guide_camera=_textured_camera(seed=11),
            device_lister=lambda: [],
            mount=FakeMountPark(start_parked=True),
            pulse_mount=pulse_mount,
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel
        panel._terrestrial_button.click()

        # Same reasoning as the star-mode calibration test above: a real,
        # non-degenerate matrix needs AXIS1's and AXIS2's own measured
        # shift to differ, not the shared static _textured_camera fixture
        # replayed unchanged for every capture. np.roll gives an exact
        # circular shift, matching measure_translation_offset()'s own
        # documented assumption for a small pulse.
        rng = np.random.default_rng(12)
        base = rng.normal(loc=500.0, scale=80.0, size=(120, 120))
        # (dy, dx) per capture, one independent sequence per camera --
        # each camera gets its own 4 calls (axis1 before/after, axis2
        # before/after), not one shared between both.
        left_shifts = iter([(0, 0), (0, 6), (0, 0), (6, 0)])
        right_shifts = iter([(0, 0), (0, 6), (0, 0), (6, 0)])

        def stepped_left_frame() -> np.ndarray:
            dy, dx = next(left_shifts)
            return np.roll(np.roll(base, dy, axis=0), dx, axis=1)

        def stepped_right_frame() -> np.ndarray:
            dy, dx = next(right_shifts)
            return np.roll(np.roll(base, dy, axis=0), dx, axis=1)

        panel._get_left_frame = stepped_left_frame
        panel._get_right_frame = stepped_right_frame

        self._run_calibration_to_completion(panel)

        assert set(panel._calibration) == {"left", "right"}
        assert "Calibration failed" not in panel._result_label.text()
        window.close()

    def test_run_calibration_reports_a_degenerate_axis_instead_of_silently_succeeding(
        self, qapp: object
    ) -> None:
        """Real report, diagnostic 0270868c: the driver reported AXIS1's
        pulse fully accepted (both motion-on and motion-off confirmed --
        see IndiMountPulseAdapter's own docstring), but produced no real,
        measurable motion -- a confidently-measured (0, 0), not a
        rejected/low-confidence one. Storing that as a "successful"
        calibration anyway used to only surface the problem later,
        confusingly, the first time a nudge button called
        compose_screen_move() and hit the same degenerate-matrix check for
        a different reason. It must be caught right here instead."""
        pulse_mount = FakeMountAdapter()
        window = MainWindow(
            _textured_camera(seed=10),
            guide_camera=_textured_camera(seed=11),
            device_lister=lambda: [],
            mount=FakeMountPark(start_parked=True),
            pulse_mount=pulse_mount,
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel
        panel._terrestrial_button.click()

        rng = np.random.default_rng(13)
        base = rng.normal(loc=500.0, scale=80.0, size=(120, 120))
        # AXIS1's before/after are identical -- no real motion, matching
        # the incident exactly -- while AXIS2's genuinely differ. One
        # independent sequence per camera, same reasoning as above.
        left_shifts = iter([(0, 0), (0, 0), (0, 0), (6, 0)])
        right_shifts = iter([(0, 0), (0, 0), (0, 0), (6, 0)])

        def stepped_left_frame() -> np.ndarray:
            dy, dx = next(left_shifts)
            return np.roll(np.roll(base, dy, axis=0), dx, axis=1)

        def stepped_right_frame() -> np.ndarray:
            dy, dx = next(right_shifts)
            return np.roll(np.roll(base, dy, axis=0), dx, axis=1)

        panel._get_left_frame = stepped_left_frame
        panel._get_right_frame = stepped_right_frame

        self._run_calibration_to_completion(panel)

        assert "left" not in panel._calibration
        assert "right" not in panel._calibration
        assert not panel._nudge_buttons["left"]["Right"].isEnabled()
        assert not panel._nudge_buttons["right"]["Right"].isEnabled()
        assert "too close to parallel" in panel._result_label.text()
        # AXIS1 (the zero one here) measured nothing on either camera --
        # the "may be a real mount/cable issue" branch, not the "confirmed
        # by the other camera" one (see the cross-camera test below).
        assert "measured no motion on either camera" in panel._result_label.text()
        assert "mount/cable issue" in panel._result_label.text()
        # Not the generic "not enough structure" wording -- that's a
        # different failure path (a rejected/low-confidence measurement),
        # not this one (a confidently-measured zero).
        assert "not enough structure" not in panel._result_label.text()
        window.close()

    def test_degenerate_axis_message_distinguishes_a_real_cross_camera_confirmation(
        self, qapp: object
    ) -> None:
        """Real report, diagnostic 0270868c: AXIS2 measured a confident
        zero on Main but a large real shift on Guide from the *same*
        pulse -- Main's much finer plate scale had likely panned that
        same real motion entirely out of frame overlap, not a mount
        problem. AXIS1 measured zero on *both* cameras that same run --
        a real, actionable "check the mount" signal, unlike AXIS2's. The
        message must tell these two apart per axis, not lump every zero
        reading into one generic "check the mount" line."""
        pulse_mount = FakeMountAdapter()
        window = MainWindow(
            _textured_camera(seed=10),
            guide_camera=_textured_camera(seed=11),
            device_lister=lambda: [],
            mount=FakeMountPark(start_parked=True),
            pulse_mount=pulse_mount,
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel
        panel._terrestrial_button.click()

        rng = np.random.default_rng(14)
        base = rng.normal(loc=500.0, scale=80.0, size=(120, 120))
        # AXIS1 identical (no motion) on both cameras; AXIS2 identical on
        # Main but genuinely differs on Guide -- exactly diagnostic
        # 0270868c's own pattern.
        left_shifts = iter([(0, 0), (0, 0), (0, 0), (0, 0)])
        right_shifts = iter([(0, 0), (0, 0), (0, 0), (6, 0)])

        def stepped_left_frame() -> np.ndarray:
            dy, dx = next(left_shifts)
            return np.roll(np.roll(base, dy, axis=0), dx, axis=1)

        def stepped_right_frame() -> np.ndarray:
            dy, dx = next(right_shifts)
            return np.roll(np.roll(base, dy, axis=0), dx, axis=1)

        panel._get_left_frame = stepped_left_frame
        panel._get_right_frame = stepped_right_frame

        self._run_calibration_to_completion(panel)

        assert "left" not in panel._calibration
        assert "right" not in panel._calibration
        lines = {line.split(":", 1)[0]: line for line in panel._result_label.text().split("\n")}
        # Main: AXIS1 zero on both cameras -- the mount/cable-issue note.
        # AXIS2 zero on Main but confirmed real on Guide -- the framing note.
        assert "RA-axis measured no motion on either camera" in lines["Main"]
        assert "mount/cable issue" in lines["Main"]
        assert "Dec-axis measured no motion here, but Guide confirms real motion" in lines["Main"]
        # Guide: only AXIS1 is zero for it too (its own AXIS2 was real) --
        # no per-axis note about AXIS2 should appear on Guide's own line
        # ("Dec-axis" still appears in the fixed "RA-axis and Dec-axis too
        # close to parallel" preamble every line has, so check for the
        # absence of an actual Dec-axis *note* specifically).
        assert "RA-axis measured no motion on either camera" in lines["Guide"]
        assert "Dec-axis measured" not in lines["Guide"]
        window.close()

    def test_diagnostic_context_reports_the_current_target_mode(self, qapp: object) -> None:
        window = self._window(mount_park=FakeMountPark(), pulse_mount=FakeMountAdapter())
        window._test_move_panel._terrestrial_button.click()
        context = window._diagnostic_context()
        assert context["mount_test_move"]["target_mode"] == "terrestrial"

    def test_nudge_button_composes_and_submits_the_predicted_pulses(self, qapp: object) -> None:
        # A hand-crafted, axis-aligned calibration (rather than one built
        # by a real Run Calibration pass) -- see class docstring for why:
        # a static single-frame camera's own calibration is degenerate.
        pulse_mount = FakeMountAdapter()
        mount_park = FakeMountPark(start_parked=True)
        window = self._window(mount_park=mount_park, pulse_mount=pulse_mount)
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        def _response(axis: MountAxis, dx_px: float, dy_px: float) -> AxisResponse:
            return AxisResponse(
                axis=axis, direction=AxisDirection.POSITIVE, duration_ms=1000,
                dx_px=dx_px, dy_px=dy_px, px_per_ms=0.0,
            )

        panel._calibration["left"] = CalibrationMatrix(
            responses={
                (MountAxis.AXIS1, AxisDirection.POSITIVE): _response(MountAxis.AXIS1, 100.0, 0.0),
                (MountAxis.AXIS2, AxisDirection.POSITIVE): _response(MountAxis.AXIS2, 0.0, 100.0),
            }
        )
        panel._update_buttons_enabled()
        assert panel._nudge_buttons["left"]["Right"].isEnabled()
        assert not panel._nudge_buttons["right"]["Right"].isEnabled()  # no matrix for "right"

        panel._nudge_buttons["left"]["Right"].click()
        # Real incident ca728d27 -- paused across the before/after bracket
        # of a nudge too, same as calibration (see the dedicated
        # calibration-pause test above).
        assert window._left_panel._auto_exposure_paused is True
        assert window._right_panel._auto_exposure_paused is True

        deadline = time.monotonic() + 5.0
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "nudge never completed"
            time.sleep(0.01)
        panel._poll()

        assert window._left_panel._auto_exposure_paused is False
        assert window._right_panel._auto_exposure_paused is False

        # axis1 rate is 100px/1000ms = 0.1 px/ms; nudge_target_px defaults
        # to 10.0 -> 10.0 / 0.1 = 100ms, axis1 only (already screen-aligned).
        settings = MountAlignmentSettings()
        assert pulse_mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 100)]
        assert pulse_mount.rate_log == [settings.rate_preset]
        assert "Main" in panel._result_label.text()
        assert "Guide" in panel._result_label.text()
        assert "failed" not in panel._result_label.text().lower()
        window.close()

    def test_nudge_button_passes_the_configured_settle_ms_to_the_runner(
        self, qapp: object
    ) -> None:
        """Real report: "calibration doesn't wait for mount to be
        stabilized" -- confirms the configured settle_ms actually reaches
        MountTestMoveRunner.submit_sequence() for a nudge, not just
        Run Calibration's own submit() calls."""
        pulse_mount = FakeMountAdapter()
        mount_park = FakeMountPark(start_parked=True)
        window = self._window(mount_park=mount_park, pulse_mount=pulse_mount)
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        def _response(axis: MountAxis, dx_px: float, dy_px: float) -> AxisResponse:
            return AxisResponse(
                axis=axis, direction=AxisDirection.POSITIVE, duration_ms=1000,
                dx_px=dx_px, dy_px=dy_px, px_per_ms=0.0,
            )

        panel._calibration["left"] = CalibrationMatrix(
            responses={
                (MountAxis.AXIS1, AxisDirection.POSITIVE): _response(MountAxis.AXIS1, 100.0, 0.0),
                (MountAxis.AXIS2, AxisDirection.POSITIVE): _response(MountAxis.AXIS2, 0.0, 100.0),
            }
        )
        panel._update_buttons_enabled()

        settle_values: list[int | None] = []
        real_submit_sequence = panel._runner.submit_sequence

        def spy_submit_sequence(
            mount_park: MountParkPort,
            mount: MountPort,
            steps: list[tuple[MountAxis, AxisDirection, int]],
            *,
            rate_preset: str | None = None,
            park_after: bool = True,
            settle_ms: int = 0,
        ) -> bool:
            settle_values.append(settle_ms)
            return real_submit_sequence(
                mount_park, mount, steps,
                rate_preset=rate_preset, park_after=park_after, settle_ms=settle_ms,
            )

        panel._runner.submit_sequence = spy_submit_sequence  # type: ignore[method-assign]

        panel._nudge_buttons["left"]["Right"].click()
        deadline = time.monotonic() + 5.0
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "nudge never completed"
            time.sleep(0.01)
        panel._poll()

        assert settle_values == [MountAlignmentSettings().settle_ms]
        window.close()

    def test_nudge_button_reports_an_error_for_a_degenerate_calibration(
        self, qapp: object
    ) -> None:
        pulse_mount = FakeMountAdapter()
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=pulse_mount
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        # AXIS1+ and AXIS2+ both moving purely +x -- degenerate, can't
        # span the image plane (see test_axis_calibration.py's own
        # dedicated coverage of compose_screen_move's ValueError itself).
        def _response(axis: MountAxis, dx_px: float) -> AxisResponse:
            return AxisResponse(
                axis=axis, direction=AxisDirection.POSITIVE, duration_ms=1000,
                dx_px=dx_px, dy_px=0.0, px_per_ms=0.0,
            )

        panel._calibration["left"] = CalibrationMatrix(
            responses={
                (MountAxis.AXIS1, AxisDirection.POSITIVE): _response(MountAxis.AXIS1, 100.0),
                (MountAxis.AXIS2, AxisDirection.POSITIVE): _response(MountAxis.AXIS2, 200.0),
            }
        )
        panel._update_buttons_enabled()

        panel._nudge_buttons["left"]["Up"].click()

        assert "Move failed" in panel._result_label.text()
        assert pulse_mount.pulse_log == []
        window.close()
