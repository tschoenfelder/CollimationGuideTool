import json
import time
from pathlib import Path

import numpy as np
import pytest
from astrotool_core.acquisition.auto_exposure import AutoExposureConfig
from astrotool_core.camera.capabilities import CameraCapabilities
from astrotool_core.camera.port import CameraPort
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo
from astrotool_core.config import load_camera_settings
from astrotool_core.diagnostics import DiagnosticService
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.port import FocuserStatus
from astrotool_core.mount.park_port import MountParkStatus
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from astrotool_core.testing.frame_factory import donut_image, single_star_image
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


def _star_camera(x: float, y: float) -> ReplayCamera:
    array = single_star_image((120, 120), x=x, y=y, peak=2000.0, sigma=2.5, background=100.0)
    return ReplayCamera.from_arrays([array], cycle=True)


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


class TestMountTestMovePanel:
    """Axis-calibration "test move" diagnostic — see MountTestMovePanel's
    docstring. FakeMountAdapter stands in for a real IndiMountPulseAdapter
    here; the INDI wire protocol itself is covered by tests/core/mount
    and tests/contracts instead. Real detection/measurement correctness
    is covered by test_mount_test_move_runner.py; these tests are about
    the panel's own wiring (connect lifecycle, the parked-gate, direction
    selection, result rendering)."""

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

    def test_starts_disconnected_with_test_move_button_disabled(self, qapp: object) -> None:
        window = self._window(mount_park=FakeMountPark(), pulse_mount=FakeMountAdapter())
        assert not window._test_move_panel._test_move_button.isEnabled()

    def test_button_disabled_when_connected_but_the_mount_is_not_parked(
        self, qapp: object
    ) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=False), pulse_mount=FakeMountAdapter()
        )
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        assert not window._test_move_panel._test_move_button.isEnabled()
        window.close()

    def test_button_enabled_once_connected_and_parked(self, qapp: object) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        assert window._test_move_panel._test_move_button.isEnabled()
        window.close()

    def test_connect_failure_keeps_the_button_disabled_and_shows_the_error(
        self, qapp: object
    ) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=True),
            pulse_mount=FakeMountAdapter(fail_connect=True),
        )
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        assert not window._test_move_panel._test_move_button.isEnabled()
        assert "failed" in window._test_move_panel._status_label.text().lower()

    def test_clicking_test_move_pulses_the_selected_direction(self, qapp: object) -> None:
        pulse_mount = FakeMountAdapter()
        window = self._window(mount_park=FakeMountPark(start_parked=True), pulse_mount=pulse_mount)
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel

        # id 2 is "E" -> (AXIS1, POSITIVE) — see _DIRECTIONS.
        panel._direction_group.button(2).setChecked(True)
        panel._test_move_button.click()

        deadline = time.monotonic() + 5.0
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "test move never completed"
            time.sleep(0.01)
        panel._poll()

        assert pulse_mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)]
        assert "Main" in panel._result_label.text()
        assert "Guide" in panel._result_label.text()
        window.close()

    def test_diagnostic_context_includes_the_last_result(self, qapp: object) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        self._connect_and_stream_cameras(window)
        window._mount_panel._connect_button.setChecked(True)
        window._test_move_panel._connect_button.setChecked(True)
        panel = window._test_move_panel
        panel._test_move_button.click()

        deadline = time.monotonic() + 5.0
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "test move never completed"
            time.sleep(0.01)
        panel._poll()

        context = window._diagnostic_context()
        assert context["mount_test_move"]["last_result"] is not None
        assert set(context["mount_test_move"]["last_result"]) == {"left", "right"}
        window.close()

    def test_closing_the_window_disconnects_the_pulse_mount(self, qapp: object) -> None:
        window = self._window(
            mount_park=FakeMountPark(start_parked=True), pulse_mount=FakeMountAdapter()
        )
        window._test_move_panel._connect_button.setChecked(True)
        window.close()
        assert not window._test_move_panel._connected
