"""GuideTool main window — live view, start/stop guiding, and the
drift/last-pulse readout.

New (Stage 7): smart_telescope's UI is a browser/JS frontend against a
FastAPI backend, so there is no PySide6 analog to port. Unlike
CollimationTool, this window doesn't own a `StreamController` directly
— `GuideController` already owns one internally and drives the
measure/correct loop on it (see its docstring); the UI just calls
`start()`/`stop()` and polls `status()`.
"""

from __future__ import annotations

from astrotool_core.camera.port import CameraPort
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from guide_tool.application.guide_controller import GuideController, GuidingStatus
from guide_tool.domain.correction_model import WouldGuidePulse
from guide_tool.ui.live_view import LiveViewLabel

_POLL_INTERVAL_MS = 150


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
    def __init__(self, camera: CameraPort) -> None:
        super().__init__()
        self.setWindowTitle("GuideTool")

        self._controller = GuideController(camera, measure_only=True)

        self._live_view = LiveViewLabel()
        self._status_label = QLabel("Start guiding to begin.")
        self._start_button = QPushButton("Start guiding")
        self._start_button.setCheckable(True)
        self._start_button.toggled.connect(self._on_toggle)

        controls = QHBoxLayout()
        controls.addWidget(self._start_button)
        controls.addStretch(1)

        layout = QVBoxLayout()
        layout.addLayout(controls)
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
        if checked:
            self._controller.start(exposure_s=0.2, cadence_s=0.2)
            self._start_button.setText("Stop guiding")
            self._timer.start()
        else:
            self._timer.stop()
            self._controller.stop()
            self._start_button.setText("Start guiding")
            self._status_label.setText("Stopped.")

    def _poll_status(self) -> None:
        status = self._controller.status()
        self._status_label.setText(_format_status(status))
        if status.latest_pixels is not None:
            error = status.source.error if status.source is not None else None
            self._live_view.set_frame(status.latest_pixels, error=error)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        self._timer.stop()
        self._controller.stop()
        super().closeEvent(event)
