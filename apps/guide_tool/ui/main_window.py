"""GuideTool main window — live view, start/stop guiding, and the
drift/last-pulse readout.

New (Stage 7): smart_telescope's UI is a browser/JS frontend against a
FastAPI backend, so there is no PySide6 analog to port. Unlike
CollimationTool, this window doesn't own a `StreamController` directly
— `GuideController` already owns one internally and drives the
measure/correct loop on it (see its docstring); the UI just calls
`start()`/`stop()` and polls `status()`.

Camera selection: same combo/Connect pattern as CollimationTool's
MainWindow (see its docstring), except GuideController binds its camera
once at construction (no camera setter), so selecting a real device
rebuilds `self._controller` around it rather than swapping a `self._camera`
reference in place.

Diagnostics (issue #10): same "Capture diagnostics" action and shared
`DiagnosticService` as CollimationTool — see its docstring. GuideController
only exposes raw pixel arrays via `GuidingStatus.latest_pixels` (no `Frame`
with exposure/header metadata at this layer), so the recent-frame buffer
here wraps each array in a minimal `Frame` for FITS export; capture
metadata is correspondingly best-effort compared to CollimationTool's.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from astropy.io import fits
from astrotool_core.camera.port import CameraPort
from astrotool_core.camera.touptek_adapter import (
    TouptekCameraAdapter,
    TouptekDeviceInfo,
)
from astrotool_core.camera.touptek_adapter import (
    list_devices as _list_touptek_devices,
)
from astrotool_core.diagnostics import DiagnosticService
from astrotool_core.frames.frame import Frame
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from guide_tool.application.guide_controller import GuideController, GuidingStatus
from guide_tool.domain.correction_model import WouldGuidePulse
from guide_tool.ui.live_view import LiveViewLabel

_POLL_INTERVAL_MS = 150
_DEMO_CAMERA_LABEL = "Demo camera (no hardware)"
_RECENT_FRAMES_KEPT = 3
_DEFAULT_MANUAL_REASON = "Manual capture from UI (no note given)"


def _default_camera_factory(camera_id: str) -> CameraPort:
    return TouptekCameraAdapter(camera_id=camera_id)


def _format_pulse(pulse: WouldGuidePulse | None) -> str:
    if pulse is None:
        return "none"
    return f"{pulse.axis.name} {pulse.direction.name} {pulse.duration_ms}ms"


def _format_status(status: GuidingStatus) -> str:
    if status.state != "running" or status.source is None:
        return "Stopped."
    source = status.source
    error = source.error
    if error is not None and error.accepted and error.error_magnitude_px is not None:
        error_text = f"{error.error_magnitude_px:.2f}px"
    else:
        error_text = "—"
    last_pulse = status.latest_pulses[-1] if status.latest_pulses else None
    return (
        f"Health: {source.health.value}  |  Error: {error_text}  |  "
        f"RMS: {status.rms_px:.2f}px  |  Last pulse: {_format_pulse(last_pulse)}"
    )


class MainWindow(QMainWindow):
    def __init__(
        self,
        camera: CameraPort,
        *,
        device_lister: Callable[[], list[TouptekDeviceInfo]] = _list_touptek_devices,
        camera_factory: Callable[[str], CameraPort] = _default_camera_factory,
        diagnostics: DiagnosticService | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("GuideTool")

        self._demo_camera = camera
        self._current_camera = camera
        self._camera_factory = camera_factory
        self._controller = GuideController(camera, measure_only=True)
        self._last_status: GuidingStatus | None = None
        self._recent_frames: deque[Frame] = deque(maxlen=_RECENT_FRAMES_KEPT)

        self._diagnostics = diagnostics or DiagnosticService(app_name="GuideTool")
        self._diagnostics.set_context_provider(self._diagnostic_context)
        self._diagnostics.set_frame_provider(lambda: list(self._recent_frames))

        self._live_view = LiveViewLabel()
        self._status_label = QLabel("Start guiding to begin.")
        self._start_button = QPushButton("Start guiding")
        self._start_button.setCheckable(True)
        self._start_button.toggled.connect(self._on_toggle)

        self._camera_combo = QComboBox()
        self._camera_combo.addItem(_DEMO_CAMERA_LABEL, None)
        for device in device_lister():
            self._camera_combo.addItem(f"{device.display_name} ({device.camera_id})", device)
        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self._on_connect_camera)
        self._camera_status_label = QLabel(f"Camera: {_DEMO_CAMERA_LABEL}")

        camera_row = QHBoxLayout()
        camera_row.addWidget(QLabel("Camera"))
        camera_row.addWidget(self._camera_combo)
        camera_row.addWidget(self._connect_button)
        camera_row.addWidget(self._camera_status_label)
        camera_row.addStretch(1)

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

        controls = QHBoxLayout()
        controls.addWidget(self._start_button)
        controls.addStretch(1)

        layout = QVBoxLayout()
        layout.addLayout(camera_row)
        layout.addLayout(controls)
        layout.addLayout(diagnostics_row)
        layout.addWidget(self._live_view, stretch=1)
        layout.addWidget(self._status_label)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.resize(720, 640)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_status)

    def _on_toggle(self, checked: bool) -> None:
        self._camera_combo.setEnabled(not checked)
        self._connect_button.setEnabled(not checked)
        if checked:
            self._controller.start(exposure_s=0.2, cadence_s=0.2)
            self._start_button.setText("Stop guiding")
            self._timer.start()
        else:
            self._timer.stop()
            self._controller.stop()
            self._start_button.setText("Start guiding")
            self._status_label.setText("Stopped.")

    def _on_connect_camera(self) -> None:
        device = self._camera_combo.currentData()
        if device is None:
            self._controller = GuideController(self._demo_camera, measure_only=True)
            self._current_camera = self._demo_camera
            self._camera_status_label.setText(f"Camera: {_DEMO_CAMERA_LABEL}")
            return

        assert isinstance(device, TouptekDeviceInfo)
        candidate = self._camera_factory(device.camera_id)
        try:
            candidate.connect()
        except ConnectionError as exc:
            self._camera_status_label.setText(f"Camera: connect failed — {exc}")
            return
        self._controller = GuideController(candidate, measure_only=True)
        self._current_camera = candidate
        self._camera_status_label.setText(f"Camera: {device.display_name}")

    def _poll_status(self) -> None:
        status = self._controller.status()
        self._last_status = status
        self._status_label.setText(_format_status(status))
        if status.latest_pixels is not None:
            error = status.source.error if status.source is not None else None
            self._live_view.set_frame(status.latest_pixels, error=error)
            self._recent_frames.append(
                Frame(pixels=status.latest_pixels, header=fits.Header(), exposure_seconds=0.0)
            )

    def _diagnostic_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {"camera_descriptor": self._current_camera.get_descriptor()}
        if self._last_status is not None:
            context["guiding_status"] = self._last_status
        return context

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
        self._timer.stop()
        self._controller.stop()
        super().closeEvent(event)
