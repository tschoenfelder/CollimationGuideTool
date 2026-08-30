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
