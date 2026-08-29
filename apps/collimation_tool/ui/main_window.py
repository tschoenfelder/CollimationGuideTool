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

Camera selection: the combo box always offers a "Demo camera" entry (index
0, userData=None — preserves the constructor-injected *camera* exactly, so
existing callers/tests are unaffected) plus one entry per
`touptek_adapter.list_devices()` result. `device_lister`/`camera_factory`
are constructor-injectable for testing without real hardware/SDK — see
FakeTouptekCamera in tests.

Diagnostics (issue #10): a "Capture diagnostics" action always available
next to the camera controls writes a UUID-identified bundle via the
shared `DiagnosticService` (`diagnostics` constructor param, injectable
for testing) — same bundle format the app's unhandled-exception boundary
uses (see main.py). This window registers itself as the service's
context/frame provider so both capture paths see the same "what was
happening" snapshot: the last measurement/recommendation, current camera
settings, and a small bounded ring buffer of the most recently captured
raw `Frame`s (`_recent_frames`, not just their displayed pixels).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from astrotool_core.acquisition.stream_controller import StreamController
from astrotool_core.camera.port import CameraPort
from astrotool_core.camera.touptek_adapter import (
    TouptekCameraAdapter,
    TouptekDeviceInfo,
)
from astrotool_core.camera.touptek_adapter import (
    list_devices as _list_touptek_devices,
)
from astrotool_core.diagnostics import DiagnosticService
from astrotool_core.frames.analysis_plane import build_analysis_plane
from astrotool_core.frames.frame import Frame
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
_DEMO_CAMERA_LABEL = "Demo camera (no hardware)"
_RECENT_FRAMES_KEPT = 3
_DEFAULT_MANUAL_REASON = "Manual capture from UI (no note given)"


def _default_camera_factory(camera_id: str) -> CameraPort:
    return TouptekCameraAdapter(camera_id=camera_id)


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
    def __init__(
        self,
        camera: CameraPort,
        *,
        device_lister: Callable[[], list[TouptekDeviceInfo]] = _list_touptek_devices,
        camera_factory: Callable[[str], CameraPort] = _default_camera_factory,
        diagnostics: DiagnosticService | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("CollimationTool")

        self._demo_camera = camera
        self._camera = camera
        self._camera_factory = camera_factory
        self._stream: StreamController | None = None
        self._last_sequence = 0
        self._controller = CollimationController()
        self._last_result: DonutAnalysisResult | None = None
        self._last_recommendation: CollimationRecommendation | None = None
        self._recent_frames: deque[Frame] = deque(maxlen=_RECENT_FRAMES_KEPT)

        self._diagnostics = diagnostics or DiagnosticService(app_name="CollimationTool")
        self._diagnostics.set_context_provider(self._diagnostic_context)
        self._diagnostics.set_frame_provider(lambda: list(self._recent_frames))

        self._live_view = LiveViewLabel()
        self._recommendation_label = QLabel("Start the stream to begin.")
        self._start_button = QPushButton("Start stream")
        self._start_button.setCheckable(True)
        self._start_button.toggled.connect(self._on_toggle_stream)

        self._camera_combo = QComboBox()
        self._camera_combo.addItem(_DEMO_CAMERA_LABEL, None)
        for device in device_lister():
            self._camera_combo.addItem(f"{device.display_name} ({device.camera_id})", device)
        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self._on_connect_camera)
        self._camera_status_label = QLabel(f"Camera: {_DEMO_CAMERA_LABEL}")

        self._exposure_spin = QDoubleSpinBox()
        self._exposure_spin.setSuffix(" ms")
        self._exposure_spin.setDecimals(1)
        self._gain_spin = QSpinBox()
        self._init_camera_controls()
        self._exposure_spin.valueChanged.connect(self._on_exposure_changed)
        self._gain_spin.valueChanged.connect(self._on_gain_changed)

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
        self._diagnostics_status_label = QLabel("")

        diagnostics_row = QHBoxLayout()
        diagnostics_row.addWidget(self._diagnostics_note, stretch=1)
        diagnostics_row.addWidget(self._capture_diagnostics_button)
        diagnostics_row.addWidget(self._diagnostics_status_label)

        controls = QHBoxLayout()
        controls.addWidget(self._start_button)
        controls.addWidget(QLabel("Exposure"))
        controls.addWidget(self._exposure_spin)
        controls.addWidget(QLabel("Gain"))
        controls.addWidget(self._gain_spin)
        controls.addStretch(1)

        layout = QVBoxLayout()
        layout.addLayout(camera_row)
        layout.addLayout(controls)
        layout.addLayout(diagnostics_row)
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
        """(Re)sync the exposure/gain spinboxes to the current self._camera.

        Safe to call more than once — a camera swap (_on_connect_camera)
        calls this again, so the spinboxes' signals are connected once in
        __init__ to indirection methods that read self._camera at call time,
        never directly to a specific camera's bound method (which would
        stack a duplicate connection per swap).
        """
        caps = self._camera.get_descriptor().capabilities
        self._exposure_spin.setRange(caps.min_exposure_ms, caps.max_exposure_ms)
        self._exposure_spin.setValue(self._camera.get_exposure_ms())
        self._gain_spin.setRange(caps.min_gain, caps.max_gain)
        self._gain_spin.setValue(self._camera.get_gain())

    def _on_exposure_changed(self, value: float) -> None:
        self._camera.set_exposure_ms(value)

    def _on_gain_changed(self, value: int) -> None:
        self._camera.set_gain(value)

    def _on_toggle_stream(self, checked: bool) -> None:
        self._camera_combo.setEnabled(not checked)
        self._connect_button.setEnabled(not checked)
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

    def _on_connect_camera(self) -> None:
        device = self._camera_combo.currentData()
        if device is None:
            self._camera = self._demo_camera
            self._camera_status_label.setText(f"Camera: {_DEMO_CAMERA_LABEL}")
            self._init_camera_controls()
            return

        assert isinstance(device, TouptekDeviceInfo)
        candidate = self._camera_factory(device.camera_id)
        try:
            candidate.connect()
        except ConnectionError as exc:
            self._camera_status_label.setText(f"Camera: connect failed — {exc}")
            return
        self._camera = candidate
        self._camera_status_label.setText(f"Camera: {device.display_name}")
        self._init_camera_controls()

    def _poll_frame(self) -> None:
        if self._stream is None:
            return
        mailbox_frame = self._stream.mailbox.wait_latest(
            after_sequence=self._last_sequence, timeout_s=0.0
        )
        if mailbox_frame is None:
            return
        self._last_sequence = mailbox_frame.sequence
        self._recent_frames.append(mailbox_frame.frame)

        plane = build_analysis_plane(mailbox_frame.frame)
        result, recommendation = self._controller.measure_and_advise(plane)
        self._last_result = result
        self._last_recommendation = recommendation
        self._live_view.set_frame(plane.mono, measurement=result.measurement)
        self._recommendation_label.setText(_format_recommendation(result, recommendation))

    def _diagnostic_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {
            "camera_descriptor": self._camera.get_descriptor(),
            "exposure_ms": self._exposure_spin.value(),
            "gain": self._gain_spin.value(),
            "streaming": self._stream is not None,
        }
        if self._last_result is not None:
            context["measurement_result"] = self._last_result
        if self._last_recommendation is not None:
            context["recommendation"] = self._last_recommendation
        return context

    def _on_capture_diagnostics(self) -> None:
        reason = self._diagnostics_note.text().strip() or _DEFAULT_MANUAL_REASON
        bundle = self._diagnostics.capture_manual(reason=reason)
        if bundle is None:
            self._diagnostics_status_label.setText("Diagnostics capture failed — see logs.")
            return
        self._diagnostics_status_label.setText(f"Diagnostics captured: {bundle.incident_id}")
        self._diagnostics_note.clear()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        self._timer.stop()
        if self._stream is not None:
            self._stream.stop_stream()
        super().closeEvent(event)
