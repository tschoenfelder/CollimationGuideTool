"""CollimationTool main window — two side-by-side camera panels plus a
shared diagnostics action.

New (Stage 7): smart_telescope's UI is a browser/JS frontend against a
FastAPI backend, so there is no PySide6 analog to port.

Two-camera layout: the left panel is the primary/collimation camera, the
right is a guide camera to watch in parallel — each is a full,
independent `CameraPanel` (its own connection, streaming, exposure/gain,
auto-exposure, and collimation measurement; see that module's docstring).
The two panels' camera pickers are cross-wired so connecting a real
device on one side removes it from the other's combo — a ToupTek camera
only allows one open handle at a time, so this isn't just a UX nicety.
The two live views are independently sized widgets, each preserving its
own aspect ratio (see `LiveViewLabel`) rather than sharing one pixel
scale — there's no requirement that the two cameras even share a native
resolution.

Deliberately not wired for Stage 7: `CollimationRecenterPolicy` (SCT
collimation screws are turned by hand; recentering the whole scope via
the mount is a separate, not-yet-decided operator workflow) and the
Tri-Bahtinov fine-collimation pathway (deferred since Stage 5 — see
docs/porting-notes.md).

Diagnostics (issue #10): one "Capture diagnostics" action for the whole
window (not duplicated per panel — capturing evidence is an app-level
concept, not a per-camera one) writes a UUID-identified bundle via the
shared `DiagnosticService` (`diagnostics` constructor param, injectable
for testing) — same bundle format the app's unhandled-exception boundary
uses (see main.py). The context/frame providers aggregate both panels'
state under "left"/"right" keys.

Guide-frame FOV overlay: the right (guide) panel's live view draws a
yellow rectangle showing where the left (main) camera's field of view
falls within it — see `collimation_tool.ui.fov_overlay`. The optical
trains' plate scale is the *master* config for this and is read exactly
once, here at startup (`main_pixel_scale_arcsec`/`guide_pixel_scale_arcsec`,
each defaulting to `astrotool_core.optics.load_pixel_scale_arcsec()`
against SmartTScope's config.toml if not given) — never re-read per
frame or per poll; a config change requires restarting the app, same as
any other startup-read config. Recomputed only when either panel's
connected camera changes (its sensor resolution is the other input).

That config-only rectangle is always centered and unrotated — it has no
way to reflect how the two cameras are actually mounted relative to each
other. "Calibrate FOV" replaces it with a real, possibly-rotated match:
`fov_registration.register_main_frame_in_guide_frame` content-matches
the two panels' latest captured frames (using the config's plate-scale
ratio only as a starting-point scale estimate) and, on a confident
match, sets the guide panel's polygon overlay via `set_fov_polygon` —
see `FovCalibrator` for why this runs on a background thread rather than
inline on the button click. An explicit, user-triggered one-shot action,
not something re-run automatically: the two scopes' relative mounting
doesn't drift frame to frame, only when the rig is physically adjusted.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrotool_core.acquisition.auto_exposure import AutoExposureConfig
from astrotool_core.camera import CameraPort, FakeCamera, TouptekDeviceInfo
from astrotool_core.camera import (
    list_devices as _list_touptek_devices,
)
from astrotool_core.diagnostics import DiagnosticService
from astrotool_core.frames.frame import Frame
from astrotool_core.optics import load_pixel_scale_arcsec
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.ui.camera_panel import CameraPanel, default_camera_factory
from collimation_tool.ui.fov_calibrator import FovCalibrator
from collimation_tool.ui.fov_overlay import compute_fov_overlay_rect
from collimation_tool.ui.fov_registration import registration_corners

_CALIBRATION_POLL_INTERVAL_MS = 200

_DEFAULT_MANUAL_REASON = "Manual capture from UI (no note given)"


class MainWindow(QMainWindow):
    def __init__(
        self,
        camera: CameraPort,
        *,
        guide_camera: CameraPort | None = None,
        device_lister: Callable[[], list[TouptekDeviceInfo]] = _list_touptek_devices,
        camera_factory: Callable[[str], CameraPort] = default_camera_factory,
        diagnostics: DiagnosticService | None = None,
        auto_exposure_config: AutoExposureConfig | None = None,
        main_pixel_scale_arcsec: float | None = None,
        guide_pixel_scale_arcsec: float | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("CollimationTool")

        # Master config, read once at startup — see module docstring's
        # "Guide-frame FOV overlay". None means "no overlay data available"
        # (e.g. no SmartTScope config.toml on this machine), not an error.
        self._main_pixel_scale_arcsec = (
            main_pixel_scale_arcsec
            if main_pixel_scale_arcsec is not None
            else load_pixel_scale_arcsec("main")
        )
        self._guide_pixel_scale_arcsec = (
            guide_pixel_scale_arcsec
            if guide_pixel_scale_arcsec is not None
            else load_pixel_scale_arcsec("guide")
        )

        self._left_panel = CameraPanel(
            camera,
            title="Main",
            device_lister=device_lister,
            camera_factory=camera_factory,
            auto_exposure_config=auto_exposure_config,
        )
        self._right_panel = CameraPanel(
            guide_camera if guide_camera is not None else FakeCamera(),
            title="Guide",
            device_lister=device_lister,
            camera_factory=camera_factory,
            auto_exposure_config=auto_exposure_config,
        )
        self._left_panel.connected_device_changed.connect(self._on_left_camera_changed)
        self._right_panel.connected_device_changed.connect(self._on_right_camera_changed)
        self._update_fov_overlay()

        self._fov_calibrator = FovCalibrator()
        self._calibrate_fov_button = QPushButton("Calibrate FOV")
        self._calibrate_fov_button.clicked.connect(self._on_calibrate_fov)
        self._calibrate_fov_status_label = QLabel("")
        self._calibrate_fov_poll_timer = QTimer(self)
        self._calibrate_fov_poll_timer.setInterval(_CALIBRATION_POLL_INTERVAL_MS)
        self._calibrate_fov_poll_timer.timeout.connect(self._poll_fov_calibration)

        self._diagnostics = diagnostics or DiagnosticService(app_name="CollimationTool")
        self._diagnostics.set_context_provider(self._diagnostic_context)
        self._diagnostics.set_frame_provider(self._all_recent_frames)

        self._diagnostics_note = QLineEdit()
        self._diagnostics_note.setPlaceholderText("What looked wrong? (optional)")
        self._capture_diagnostics_button = QPushButton("Capture diagnostics")
        self._capture_diagnostics_button.clicked.connect(self._on_capture_diagnostics)
        # A read-only QLineEdit (not a QLabel) so the incident UUID is
        # selectable/copyable via normal text-field interaction — see
        # issue #11. A "Copy" button covers the one-click case too.
        self._diagnostics_status_label = QLineEdit("")
        self._diagnostics_status_label.setReadOnly(True)
        self._diagnostics_copy_button = QPushButton("Copy")
        self._diagnostics_copy_button.clicked.connect(self._on_copy_diagnostics_status)

        diagnostics_row = QHBoxLayout()
        diagnostics_row.addWidget(self._diagnostics_note, stretch=1)
        diagnostics_row.addWidget(self._capture_diagnostics_button)
        diagnostics_row.addWidget(self._diagnostics_status_label, stretch=1)
        diagnostics_row.addWidget(self._diagnostics_copy_button)

        calibration_row = QHBoxLayout()
        calibration_row.addWidget(self._calibrate_fov_button)
        calibration_row.addWidget(self._calibrate_fov_status_label, stretch=1)

        panels_row = QHBoxLayout()
        panels_row.addWidget(self._left_panel, stretch=1)
        panels_row.addWidget(self._right_panel, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(diagnostics_row)
        layout.addLayout(calibration_row)
        layout.addLayout(panels_row, stretch=1)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.resize(1360, 700)

    def _on_left_camera_changed(self, device: object) -> None:
        excluded = device.camera_id if isinstance(device, TouptekDeviceInfo) else None
        self._right_panel.refresh_camera_list(excluded)
        self._update_fov_overlay()

    def _on_right_camera_changed(self, device: object) -> None:
        excluded = device.camera_id if isinstance(device, TouptekDeviceInfo) else None
        self._left_panel.refresh_camera_list(excluded)
        self._update_fov_overlay()

    def _update_fov_overlay(self) -> None:
        """Recompute the guide-frame FOV rectangle — see module docstring.
        Called whenever either panel's connected camera changes (its
        sensor resolution is the other input this needs); never re-reads
        the optical-train config itself, which was read once at startup."""
        if self._main_pixel_scale_arcsec is None or self._guide_pixel_scale_arcsec is None:
            self._right_panel.set_fov_overlay(None)
            return
        main_caps = self._left_panel.camera_descriptor().capabilities
        guide_caps = self._right_panel.camera_descriptor().capabilities
        rect = compute_fov_overlay_rect(
            main_pixel_scale_arcsec=self._main_pixel_scale_arcsec,
            main_sensor_width_px=main_caps.sensor_width_px,
            main_sensor_height_px=main_caps.sensor_height_px,
            guide_pixel_scale_arcsec=self._guide_pixel_scale_arcsec,
            guide_sensor_width_px=guide_caps.sensor_width_px,
            guide_sensor_height_px=guide_caps.sensor_height_px,
        )
        self._right_panel.set_fov_overlay(rect)
        # A previous calibration was matched against whichever camera was
        # connected before — no longer meaningful once either side's
        # camera changes (different resolution/content entirely).
        self._right_panel.set_fov_polygon(None)

    def _on_calibrate_fov(self) -> None:
        """Kick off a one-shot content-matching calibration — see module
        docstring's "Calibrate FOV". Runs on FovCalibrator's background
        thread; _poll_fov_calibration picks up the result."""
        main_mono = self._left_panel.latest_mono_frame()
        guide_mono = self._right_panel.latest_mono_frame()
        if main_mono is None or guide_mono is None:
            self._calibrate_fov_status_label.setText(
                "Start both streams first — no captured frame to match yet."
            )
            return
        if not self._main_pixel_scale_arcsec or not self._guide_pixel_scale_arcsec:
            # Covers both "no config found" (None) and a given-but-invalid
            # value (0.0) — either way there's nothing to divide by for a
            # starting scale estimate.
            self._calibrate_fov_status_label.setText(
                "No optical-train plate-scale config available — can't estimate a starting scale."
            )
            return
        approx_scale = self._main_pixel_scale_arcsec / self._guide_pixel_scale_arcsec
        started = self._fov_calibrator.submit(main_mono, guide_mono, approx_scale=approx_scale)
        if not started:
            return  # a calibration is already running
        self._calibrate_fov_button.setEnabled(False)
        self._calibrate_fov_status_label.setText("Calibrating…")
        self._calibrate_fov_poll_timer.start()

    def _poll_fov_calibration(self) -> None:
        outcome = self._fov_calibrator.take_latest()
        if outcome is None:
            # Still running — see the real bug this progress reporting
            # was added for ("Calibration started but working without any
            # status on progress"): the search genuinely takes on the
            # order of two real minutes (see fov_registration's
            # docstring), and a static "Calibrating…" message for that
            # long is indistinguishable from a hang.
            progress = self._fov_calibrator.latest_progress()
            if progress is not None:
                completed, total = progress
                percent = (completed / total * 100.0) if total else 0.0
                self._calibrate_fov_status_label.setText(
                    f"Calibrating… {completed}/{total} ({percent:.0f}%)"
                )
            return
        self._calibrate_fov_poll_timer.stop()
        self._calibrate_fov_button.setEnabled(True)
        if outcome.result is None:
            self._calibrate_fov_status_label.setText(
                "No confident match found — keeping the previous overlay."
            )
            return
        result = outcome.result
        self._calibrate_fov_status_label.setText(
            f"Calibrated: rotation {result.rotation_deg:.1f}°, "
            f"scale {result.scale:.4f}, score {result.score:.2f}"
        )
        self._right_panel.set_fov_polygon(registration_corners(result))

    def _diagnostic_context(self) -> dict[str, Any]:
        return {
            "left": self._left_panel.diagnostic_context(),
            "right": self._right_panel.diagnostic_context(),
        }

    def _all_recent_frames(self) -> list[Frame]:
        return self._left_panel.recent_frames() + self._right_panel.recent_frames()

    def _on_capture_diagnostics(self) -> None:
        reason = self._diagnostics_note.text().strip() or _DEFAULT_MANUAL_REASON
        bundle = self._diagnostics.capture_manual(reason=reason)
        if bundle is None:
            self._diagnostics_status_label.setText("Diagnostics capture failed — see logs.")
            return
        # Just the raw UUID (not "Diagnostics captured: <uuid>" prose) — the
        # field exists so this can be selected/copied cleanly (issue #11);
        # the log line above still carries the human-readable framing.
        self._diagnostics_status_label.setText(bundle.incident_id)
        self._diagnostics_note.clear()

    def _on_copy_diagnostics_status(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._diagnostics_status_label.text())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        self._calibrate_fov_poll_timer.stop()
        self._left_panel.stop()
        self._right_panel.stop()
        super().closeEvent(event)
