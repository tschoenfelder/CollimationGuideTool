"""CameraPanel — one camera's full live-view/controls/measurement UI.

Extracted from what used to be all of MainWindow, so CollimationTool can
show two cameras side by side (a primary/collimation camera and a guide
camera) without duplicating this logic. Each panel is fully independent:
its own camera connection, StreamController, CollimationController,
exposure/gain/auto-exposure state, and poll timer:

- Camera selection: same demo-plus-devices combo as before (see
  `astrotool_core.camera.build_camera_choices`), but a panel can be told
  to exclude a device connected on another panel via
  `refresh_camera_list()` — see `connected_device_changed` below.
- Auto exposure/gain: unchanged from the original MainWindow — see
  `astrotool_core.acquisition.auto_exposure`'s docstring.

Two-panels-can't-share-one-camera: `connected_device_changed` fires
whenever this panel's connected camera changes (`None` for the demo
camera, a `TouptekDeviceInfo` otherwise). MainWindow cross-wires two
panels' signals to each other's `refresh_camera_list()`, so connecting a
real device on one side removes it from the other side's combo — the
underlying hardware constraint is that a ToupTek camera only allows one
open handle at a time, so this isn't just a UX nicety.

Diagnostics stay owned by MainWindow, not duplicated per panel — a
"Capture diagnostics" action conceptually belongs to the whole app, not
one camera. `diagnostic_context()`/`recent_frames()` are this panel's
contribution to that aggregate.

Analysis runs off the UI thread: measured against a real camera frame,
the measurement+stretch pipeline took ~900ms — inline on the poll timer
that blocked the whole window's event loop, for both panels. See
`FrameAnalyzer`. `_poll_frame()` only ever submits/collects; it never
runs the pipeline itself.

Mono vs. color: a color camera's raw frame is a Bayer mosaic, not a
valid mono plane — `_camera.is_color_sensor()`/`get_bayer_pattern()`
(see `CameraPort`) tell `FrameAnalyzer` whether and how to demosaic it
before analysis. Demosaicing was implemented and is unit-tested, but
not yet proven against a real color sensor — no color camera was free
to test against during development (see the commit that introduced
this for which real cameras were checked and their result).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np
from astrotool_core.acquisition.auto_exposure import AutoExposureConfig, compute_auto_exposure
from astrotool_core.acquisition.stream_controller import StreamController
from astrotool_core.camera import (
    DEMO_CAMERA_LABEL,
    CameraDescriptor,
    CameraPort,
    TouptekCameraAdapter,
    TouptekDeviceInfo,
    build_camera_choices,
)
from astrotool_core.camera import (
    list_devices as _list_touptek_devices,
)
from astrotool_core.frames import demosaic, rgb_to_luma
from astrotool_core.frames.frame import Frame
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.application.collimation_controller import CollimationController
from collimation_tool.domain.collimation_measurement import DonutAnalysisResult
from collimation_tool.domain.collimation_state import CollimationRecommendation
from collimation_tool.ui.fov_overlay import FovOverlayRect
from collimation_tool.ui.frame_analyzer import FrameAnalyzer
from collimation_tool.ui.live_view import LiveViewLabel

_POLL_INTERVAL_MS = 100
_RECENT_FRAMES_KEPT = 3


def default_camera_factory(camera_id: str) -> CameraPort:
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


class CameraPanel(QWidget):
    #: Emits the newly-connected TouptekDeviceInfo, or None for the demo
    #: camera — see module docstring's "two panels can't share one camera".
    connected_device_changed = Signal(object)

    def __init__(
        self,
        camera: CameraPort,
        *,
        title: str,
        device_lister: Callable[[], list[TouptekDeviceInfo]] = _list_touptek_devices,
        camera_factory: Callable[[str], CameraPort] = default_camera_factory,
        auto_exposure_config: AutoExposureConfig | None = None,
    ) -> None:
        super().__init__()
        self._device_lister = device_lister
        self._camera_factory = camera_factory
        self._excluded_camera_id: str | None = None

        self._demo_camera = camera
        self._camera = camera
        self._connected_device: TouptekDeviceInfo | None = None
        self._stream: StreamController | None = None
        self._last_sequence = 0
        self._controller = CollimationController()
        self._analyzer = FrameAnalyzer(self._controller)
        self._last_result: DonutAnalysisResult | None = None
        self._last_recommendation: CollimationRecommendation | None = None
        self._recent_frames: deque[Frame] = deque(maxlen=_RECENT_FRAMES_KEPT)
        self._auto_exposure_config = auto_exposure_config or AutoExposureConfig()
        #: Set by MainWindow via set_fov_overlay() — see its docstring's
        #: "Guide-frame FOV overlay". None on the left/main panel always;
        #: on the right/guide panel, None means no overlay data available.
        self._fov_rect: FovOverlayRect | None = None
        #: Set by MainWindow via set_fov_polygon() once a "Calibrate FOV"
        #: run finds a confident content match — see fov_registration and
        #: MainWindow's docstring. Takes precedence over _fov_rect (a
        #: measured, possibly-rotated match beats the config-only centered
        #: placeholder) when both are set.
        self._fov_polygon: list[tuple[float, float]] | None = None

        self._title_label = QLabel(f"<b>{title}</b>")
        self._live_view = LiveViewLabel()
        self._recommendation_label = QLabel("Start the stream to begin.")
        self._start_button = QPushButton("Start stream")
        self._start_button.setCheckable(True)
        self._start_button.toggled.connect(self._on_toggle_stream)

        self._camera_combo = QComboBox()
        for choice in build_camera_choices(device_lister()):
            self._camera_combo.addItem(choice.label, choice.device)
        self._connect_button = QPushButton("Connect")
        self._connect_button.clicked.connect(self._on_connect_camera)
        self._camera_status_label = QLabel(f"Camera: {DEMO_CAMERA_LABEL}")

        self._exposure_spin = QDoubleSpinBox()
        self._exposure_spin.setSuffix(" ms")
        self._exposure_spin.setDecimals(1)
        self._gain_spin = QSpinBox()
        self._init_camera_controls()
        self._exposure_spin.valueChanged.connect(self._on_exposure_changed)
        self._gain_spin.valueChanged.connect(self._on_gain_changed)

        self._auto_exposure_checkbox = QCheckBox("Auto exposure/gain")
        self._auto_exposure_checkbox.toggled.connect(self._on_auto_exposure_toggled)

        camera_row = QHBoxLayout()
        camera_row.addWidget(QLabel("Camera"))
        camera_row.addWidget(self._camera_combo)
        camera_row.addWidget(self._connect_button)
        camera_row.addWidget(self._camera_status_label)
        camera_row.addStretch(1)

        controls = QHBoxLayout()
        controls.addWidget(self._start_button)
        controls.addWidget(QLabel("Exposure"))
        controls.addWidget(self._exposure_spin)
        controls.addWidget(QLabel("Gain"))
        controls.addWidget(self._gain_spin)
        controls.addWidget(self._auto_exposure_checkbox)
        controls.addStretch(1)

        layout = QVBoxLayout()
        layout.addWidget(self._title_label)
        layout.addLayout(camera_row)
        layout.addLayout(controls)
        layout.addWidget(self._live_view, stretch=1)
        layout.addWidget(self._recommendation_label)
        self.setLayout(layout)

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

    def _on_auto_exposure_toggled(self, checked: bool) -> None:
        self._exposure_spin.setEnabled(not checked)
        self._gain_spin.setEnabled(not checked)
        if checked:
            self._gain_spin.setValue(self._auto_exposure_config.default_gain)

    def _apply_auto_exposure(self, frame: Frame) -> None:
        result = compute_auto_exposure(
            frame.pixels,
            bit_depth=frame.bit_depth,
            current_exposure_ms=self._camera.get_exposure_ms(),
            current_gain=self._camera.get_gain(),
            capabilities=self._camera.get_descriptor().capabilities,
            config=self._auto_exposure_config,
        )
        if result.changed:
            self._exposure_spin.setValue(result.exposure_ms)
            self._gain_spin.setValue(result.gain)

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
            self._connected_device = None
            self._camera_status_label.setText(f"Camera: {DEMO_CAMERA_LABEL}")
            self._init_camera_controls()
            self.connected_device_changed.emit(None)
            return

        assert isinstance(device, TouptekDeviceInfo)
        candidate = self._camera_factory(device.camera_id)
        try:
            candidate.connect()
        except ConnectionError as exc:
            self._camera_status_label.setText(f"Camera: connect failed — {exc}")
            return
        self._camera = candidate
        self._connected_device = device
        self._camera_status_label.setText(f"Camera: {device.display_name}")
        self._init_camera_controls()
        self.connected_device_changed.emit(device)

    def refresh_camera_list(self, excluded_camera_id: str | None) -> None:
        """Rebuild the combo excluding a device connected on another panel.

        Preserves the current selection when it's still available; if the
        currently *selected* (not yet connected) item just became excluded,
        falls back to the demo camera entry.

        Matches the previous selection by ``camera_id`` (a plain string)
        rather than via ``QComboBox.findData()``: PySide6 wraps a stored
        Python object in a QVariant, and ``findData()`` does not reliably
        fall back to the object's own ``__eq__`` for value equality — found
        via real-hardware testing, where `list_devices()` builds a fresh
        `TouptekDeviceInfo` instance on every call, so `findData()` almost
        always missed even an exact value match (it happened to "work" in
        tests that reused one fixed device list — the exact same object
        instances — masking this).
        """
        self._excluded_camera_id = excluded_camera_id
        current = self._camera_combo.currentData()
        current_camera_id = current.camera_id if isinstance(current, TouptekDeviceInfo) else None
        self._camera_combo.blockSignals(True)
        try:
            self._camera_combo.clear()
            devices = [d for d in self._device_lister() if d.camera_id != excluded_camera_id]
            for choice in build_camera_choices(devices):
                self._camera_combo.addItem(choice.label, choice.device)
            restore_at = 0
            if current_camera_id is not None:
                for i in range(1, self._camera_combo.count()):
                    item = self._camera_combo.itemData(i)
                    if isinstance(item, TouptekDeviceInfo) and item.camera_id == current_camera_id:
                        restore_at = i
                        break
            self._camera_combo.setCurrentIndex(restore_at)
        finally:
            self._camera_combo.blockSignals(False)

    def _poll_frame(self) -> None:
        """Cheap by design — see module docstring. Never runs the analysis
        pipeline itself: submits the latest captured frame to `_analyzer`
        (a no-op if a previous analysis is still running) and picks up
        whichever analysis most recently finished, if any."""
        if self._stream is None:
            return
        mailbox_frame = self._stream.mailbox.wait_latest(
            after_sequence=self._last_sequence, timeout_s=0.0
        )
        if mailbox_frame is not None:
            self._last_sequence = mailbox_frame.sequence
            self._recent_frames.append(mailbox_frame.frame)

            if self._auto_exposure_checkbox.isChecked():
                self._apply_auto_exposure(mailbox_frame.frame)

            self._analyzer.submit(
                mailbox_frame.frame,
                is_color=self._camera.is_color_sensor(),
                bayer_pattern=self._camera.get_bayer_pattern(),
            )

        outcome = self._analyzer.take_latest()
        if outcome is not None:
            self._last_result = outcome.result
            self._last_recommendation = outcome.recommendation
            self._live_view.set_stretched_frame(
                outcome.stretched,
                measurement=outcome.result.measurement,
                fov_rect=self._fov_rect,
                fov_polygon=self._fov_polygon,
            )
            self._recommendation_label.setText(
                _format_recommendation(outcome.result, outcome.recommendation)
            )

    def diagnostic_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {
            "camera_descriptor": self._camera.get_descriptor(),
            "exposure_ms": self._exposure_spin.value(),
            "gain": self._gain_spin.value(),
            "streaming": self._stream is not None,
            "auto_exposure_enabled": self._auto_exposure_checkbox.isChecked(),
        }
        if self._last_result is not None:
            context["measurement_result"] = self._last_result
        if self._last_recommendation is not None:
            context["recommendation"] = self._last_recommendation
        return context

    def recent_frames(self) -> list[Frame]:
        return list(self._recent_frames)

    def camera_descriptor(self) -> CameraDescriptor:
        return self._camera.get_descriptor()

    def set_fov_overlay(self, rect: FovOverlayRect | None) -> None:
        """Set (or clear, with None) the yellow FOV rectangle this panel's
        live view draws — see MainWindow's docstring. Takes effect on the
        next polled frame; does not force an immediate redraw of a
        currently-displayed frame."""
        self._fov_rect = rect

    def set_fov_polygon(self, corners: list[tuple[float, float]] | None) -> None:
        """Set (or clear, with None) the calibrated FOV polygon this
        panel's live view draws — see MainWindow's "Calibrate FOV" and
        `set_fov_overlay`'s docstring (this takes precedence when both
        are set). Takes effect on the next polled frame."""
        self._fov_polygon = corners

    def latest_mono_frame(self) -> np.ndarray | None:
        """The most recently captured frame's mono representation
        (demosaiced luma for a color camera, raw pixels for mono) — for
        one-shot FOV calibration (see `fov_registration`/MainWindow's
        "Calibrate FOV"). None if nothing has been captured yet.

        Deliberately reads the raw captured `Frame` directly rather than
        FrameAnalyzer's latest analysis outcome: that pipeline runs
        asynchronously and can lag behind by however long a full
        measure_and_advise pass takes, whereas calibration just needs
        *a* recent, real frame from each camera.
        """
        if not self._recent_frames:
            return None
        frame = self._recent_frames[-1]
        if self._camera.is_color_sensor():
            rgb = demosaic(frame.pixels, self._camera.get_bayer_pattern())
            mono: np.ndarray = rgb_to_luma(rgb)
            return mono
        pixels: np.ndarray = frame.pixels
        return pixels

    def stop(self) -> None:
        """Stop streaming/polling. Safe to call whether or not streaming."""
        self._timer.stop()
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream = None
