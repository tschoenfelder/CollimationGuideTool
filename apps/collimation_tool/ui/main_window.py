"""CollimationTool main window — live view, exposure/gain controls, and
the rough-collimation recommendation readout.

New (Stage 7): smart_telescope's UI is a browser/JS frontend against a
FastAPI backend, so there is no PySide6 analog to port. This window
owns the camera's `StreamController` directly (unlike GuideTool, where
`GuideController` owns its own internal stream) because
`CollimationController` is a pure per-frame measure/advise API with no
run loop of its own (see its docstring) — someone has to drive it, and
for this app that's the UI.

Deliberately not wired for Stage 7: `CollimationRecenterPolicy` (SCT
collimation screws are turned by hand; recentering the whole scope via
the mount is a separate, not-yet-decided operator workflow) and the
Tri-Bahtinov fine-collimation pathway (deferred since Stage 5 — see
docs/porting-notes.md).
"""

from __future__ import annotations

from astrotool_core.acquisition.stream_controller import StreamController
from astrotool_core.camera.port import CameraPort
from astrotool_core.frames.analysis_plane import build_analysis_plane
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.application.collimation_controller import CollimationController
from collimation_tool.domain.collimation_measurement import DonutAnalysisResult
from collimation_tool.domain.collimation_state import CollimationRecommendation
from collimation_tool.ui.live_view import LiveViewLabel

_POLL_INTERVAL_MS = 100


def _format_recommendation(
    result: DonutAnalysisResult, recommendation: CollimationRecommendation | None
) -> str:
    if result.measurement is None:
        return f"No measurement — {result.reason}"
    if recommendation is None:
        return (
            f"Error {result.measurement.error_magnitude_px:.1f}px — "
            "no screw calibration learned yet"
        )
    if not recommendation.is_actionable:
        return f"Close to collimated (confidence {recommendation.confidence:.0%})"
    return (
        f"Turn screw {recommendation.screw_id} "
        f"{recommendation.turn_direction.value.replace('_', ' ')}, "
        f"{recommendation.adjustment_size.value} "
        f"(confidence {recommendation.confidence:.0%})"
    )


class MainWindow(QMainWindow):
    def __init__(self, camera: CameraPort) -> None:
        super().__init__()
        self.setWindowTitle("CollimationTool")

        self._camera = camera
        self._stream: StreamController | None = None
        self._last_sequence = 0
        self._controller = CollimationController()

        self._live_view = LiveViewLabel()
        self._recommendation_label = QLabel("Start the stream to begin.")
        self._start_button = QPushButton("Start stream")
        self._start_button.setCheckable(True)
        self._start_button.toggled.connect(self._on_toggle_stream)

        self._exposure_spin = QDoubleSpinBox()
        self._exposure_spin.setSuffix(" ms")
        self._exposure_spin.setDecimals(1)
        self._gain_spin = QSpinBox()
        self._init_camera_controls()

        controls = QHBoxLayout()
        controls.addWidget(self._start_button)
        controls.addWidget(QLabel("Exposure"))
        controls.addWidget(self._exposure_spin)
        controls.addWidget(QLabel("Gain"))
        controls.addWidget(self._gain_spin)
        controls.addStretch(1)

        layout = QVBoxLayout()
        layout.addLayout(controls)
        layout.addWidget(self._live_view, stretch=1)
        layout.addWidget(self._recommendation_label)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.resize(720, 640)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_frame)

    def _init_camera_controls(self) -> None:
        caps = self._camera.get_descriptor().capabilities
        self._exposure_spin.setRange(caps.min_exposure_ms, caps.max_exposure_ms)
        self._exposure_spin.setValue(self._camera.get_exposure_ms())
        self._exposure_spin.valueChanged.connect(self._camera.set_exposure_ms)
        self._gain_spin.setRange(caps.min_gain, caps.max_gain)
        self._gain_spin.setValue(self._camera.get_gain())
        self._gain_spin.valueChanged.connect(self._camera.set_gain)

    def _on_toggle_stream(self, checked: bool) -> None:
        if checked:
            self._camera.connect()
            self._stream = StreamController(self._camera, name="collimation")
            self._stream.start_stream(self._exposure_spin.value() / 1000.0, cadence_s=0.2)
            self._last_sequence = 0
            self._start_button.setText("Stop stream")
            self._timer.start()
        else:
            self._timer.stop()
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream = None
            self._start_button.setText("Start stream")
            self._recommendation_label.setText("Stream stopped.")

    def _poll_frame(self) -> None:
        if self._stream is None:
            return
        mailbox_frame = self._stream.mailbox.wait_latest(
            after_sequence=self._last_sequence, timeout_s=0.0
        )
        if mailbox_frame is None:
            return
        self._last_sequence = mailbox_frame.sequence

        plane = build_analysis_plane(mailbox_frame.frame)
        result, recommendation = self._controller.measure_and_advise(plane)
        self._live_view.set_frame(plane.mono, measurement=result.measurement)
        self._recommendation_label.setText(_format_recommendation(result, recommendation))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        self._timer.stop()
        if self._stream is not None:
            self._stream.stop_stream()
        super().closeEvent(event)
