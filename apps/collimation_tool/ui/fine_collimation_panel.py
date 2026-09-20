"""FineCollimationPanel — issue #21 (Stage 7): the first UI display for
the maskless fine-collimation measurement, wiring together Stages 1-6
via FineCollimationController/FineCollimationRunner. Requirements doc's
own required elements: enlarged focused-star stack, star center,
measured asymmetry/error vector, confidence, radial profile,
sufficient-sampling/SNR indication, current fine-collimation state --
and it "shall clearly distinguish ROUGH COLLIMATION from FINE
COLLIMATION" ("the existing donut result shall not be presented as
proof of final fine collimation").

AC 7.2's "no false green" is enforced structurally, not just by
message text: `is_collimated_style_active` (and the visual style driven
from it) is only ever `True` when the symmetry measurement is
`SymmetryStatus.FINE_COLLIMATED` AND the upstream radial profile was
itself sufficiently sampled -- every other status (including a failed
run) renders the neutral/warning style plus an explanatory message,
never the collimated one.

No charting library anywhere in this codebase (confirmed by grep) --
`RadialProfilePlotWidget` is a small, purpose-built hand-drawn
`QPainter` widget, matching `live_view.py`'s own established technique
(reuses `stretch_to_uint8` directly rather than reimplementing it) for
the stacked-star display.
"""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
from astrotool_core.diffraction.optical_reference_model import OpticalConfig
from astrotool_core.diffraction.symmetry_measurement import SymmetryStatus
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QResizeEvent
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.application.fine_collimation_controller import (
    FineCollimationController,
    FineCollimationOutcome,
    GuideReacquirer,
)
from collimation_tool.application.star_acquisition import FocusedStarAcquisition
from collimation_tool.domain.target_mode import CollimationTargetMode
from collimation_tool.ui.fine_collimation_runner import FineCollimationRunner
from collimation_tool.ui.live_view import stretch_to_uint8

_POLL_INTERVAL_MS = 250
_DEFAULT_ROI_SIZE = (240, 240)
_DEFAULT_SAMPLE_COUNT = 8

_VECTOR_COLOR = QColor(255, 90, 90)
_CENTER_COLOR = QColor(80, 220, 255)
_PROFILE_COLOR = QColor(80, 220, 255)
_MEASURED_RING_COLOR = QColor(255, 90, 90)
_EXPECTED_RING_COLOR = QColor(255, 210, 60)

_COLLIMATED_STYLE = "background-color: #1b5e20; color: white; padding: 2px;"
_WARNING_STYLE = "background-color: #4a3b00; color: white; padding: 2px;"

#: The scaled arrow length (px, at 1:1 with the displayed image) drawn
#: for a magnitude of 1.0 -- an implementation decision (the magnitude
#: itself is a dimensionless normalized value, not a pixel distance).
_VECTOR_LENGTH_SCALE_PX = 80.0


class _StackedStarView(QLabel):
    """Displays the stacked focused-star image plus its measured center
    and asymmetry/error vector -- reuses `live_view.stretch_to_uint8`
    directly rather than reimplementing the percentile-stretch."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(240, 240)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: black;")
        self.setText("No stacked frame yet")
        self._base_pixmap: QPixmap | None = None
        self.has_image = False

    def set_display(
        self,
        stacked: np.ndarray,
        *,
        center: tuple[float, float] | None,
        direction_deg: float | None,
        magnitude: float | None,
    ) -> None:
        stretched = np.ascontiguousarray(stretch_to_uint8(stacked))
        height, width = stretched.shape
        image = QImage(
            stretched.tobytes(), width, height, width, QImage.Format.Format_Grayscale8
        ).copy()
        pixmap = QPixmap.fromImage(image)

        if center is not None:
            painter = QPainter(pixmap)
            try:
                cx, cy = int(center[0]), int(center[1])
                painter.setPen(QPen(_CENTER_COLOR, 2))
                cross = 8
                painter.drawLine(cx - cross, cy, cx + cross, cy)
                painter.drawLine(cx, cy - cross, cx, cy + cross)

                if direction_deg is not None and magnitude is not None:
                    length = magnitude * _VECTOR_LENGTH_SCALE_PX
                    angle_rad = math.radians(direction_deg)
                    end_x = cx + length * math.cos(angle_rad)
                    end_y = cy + length * math.sin(angle_rad)
                    painter.setPen(QPen(_VECTOR_COLOR, 2))
                    painter.drawLine(cx, cy, int(end_x), int(end_y))
            finally:
                painter.end()

        self._base_pixmap = pixmap
        self.has_image = True
        self._update_scaled_pixmap()

    def clear_display(self) -> None:
        self._base_pixmap = None
        self.has_image = False
        self.setPixmap(QPixmap())
        self.setText("No stacked frame yet")

    def _update_scaled_pixmap(self) -> None:
        if self._base_pixmap is None or self._base_pixmap.isNull():
            return
        scaled = self._base_pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.setPixmap(scaled)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 — Qt override
        super().resizeEvent(event)
        self._update_scaled_pixmap()


class RadialProfilePlotWidget(QWidget):
    """A small, purpose-built hand-drawn plot of normalized intensity
    vs. radius -- see module docstring for why this isn't a general
    charting component or a new dependency."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(240, 160)
        self._radii: tuple[float, ...] = ()
        self._normalized: tuple[float, ...] = ()
        self._measured_ring_radius_px: float | None = None
        self._expected_ring_radius_px: float | None = None
        self.has_profile = False

    def set_profile(
        self,
        radii_px: tuple[float, ...],
        normalized_intensity: tuple[float, ...],
        *,
        measured_ring_radius_px: float | None,
        expected_ring_radius_px: float | None,
    ) -> None:
        self._radii = radii_px
        self._normalized = normalized_intensity
        self._measured_ring_radius_px = measured_ring_radius_px
        self._expected_ring_radius_px = expected_ring_radius_px
        self.has_profile = bool(radii_px)
        self.update()

    def clear_profile(self) -> None:
        self._radii = ()
        self._normalized = ()
        self._measured_ring_radius_px = None
        self._expected_ring_radius_px = None
        self.has_profile = False
        self.update()

    def paintEvent(self, event: object) -> None:  # noqa: N802 — Qt override
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(20, 20, 20))
            if not self._radii:
                return

            width, height = self.width(), self.height()
            max_r = max(self._radii)
            max_i = max(max(self._normalized), 1e-6)

            def to_point(r: float, intensity: float) -> tuple[int, int]:
                x = int((r / max_r) * (width - 1)) if max_r > 0 else 0
                y = int(height - 1 - (intensity / max_i) * (height - 1))
                return x, y

            painter.setPen(QPen(_PROFILE_COLOR, 2))
            points = [to_point(r, i) for r, i in zip(self._radii, self._normalized, strict=True)]
            for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
                painter.drawLine(x0, y0, x1, y1)

            if self._measured_ring_radius_px is not None and max_r > 0:
                x = int((self._measured_ring_radius_px / max_r) * (width - 1))
                painter.setPen(QPen(_MEASURED_RING_COLOR, 1, Qt.PenStyle.DashLine))
                painter.drawLine(x, 0, x, height - 1)

            if self._expected_ring_radius_px is not None and max_r > 0:
                x = int((self._expected_ring_radius_px / max_r) * (width - 1))
                painter.setPen(QPen(_EXPECTED_RING_COLOR, 1, Qt.PenStyle.DashLine))
                painter.drawLine(x, 0, x, height - 1)
        finally:
            painter.end()


class FineCollimationPanel(QWidget):
    #: Fires (with the new `CollimationTargetMode`) whenever the target
    #: mode changes -- MainWindow mirrors it onto the rough-collimation view.
    target_mode_changed = Signal(object)

    def __init__(
        self,
        *,
        get_frame: Callable[[], np.ndarray | None],
        optical_config: OpticalConfig | None = None,
        roi_size: tuple[int, int] = _DEFAULT_ROI_SIZE,
        sample_count: int = _DEFAULT_SAMPLE_COUNT,
        title: str = "Fine Collimation",
        guide_reacquirer: GuideReacquirer | None = None,
    ) -> None:
        super().__init__()
        self._get_frame = get_frame
        self._guide_reacquirer = guide_reacquirer
        self._target_mode = CollimationTargetMode.NATURAL_STAR
        self._last_outcome: FineCollimationOutcome | None = None
        self._optical_config = optical_config if optical_config is not None else OpticalConfig()
        self._roi_size = roi_size
        self._sample_count = sample_count
        self._runner = FineCollimationRunner()
        self._running = False
        self.is_collimated_style_active = False

        self._title_label = QLabel(f"<b>{title}</b>")
        self._run_button = QPushButton("Run Fine Collimation")
        self._run_button.clicked.connect(self._on_run_clicked)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.clicked.connect(self._on_cancel_clicked)
        self._cancel_button.setEnabled(False)
        self._status_label = QLabel("Not yet measured.")
        self._target_mode_combo = QComboBox()
        for mode in CollimationTargetMode:
            self._target_mode_combo.addItem(mode.label, mode)
        self._target_mode_combo.currentIndexChanged.connect(self._on_target_mode_selected)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._target_mode_combo)
        top_row.addWidget(self._run_button)
        top_row.addWidget(self._cancel_button)
        top_row.addWidget(self._status_label, stretch=1)

        self._star_view = _StackedStarView()
        self._profile_widget = RadialProfilePlotWidget()

        display_row = QHBoxLayout()
        display_row.addWidget(self._star_view, stretch=1)
        display_row.addWidget(self._profile_widget, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(display_row)
        self.setLayout(layout)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self._poll_run)

    def _on_run_clicked(self) -> None:
        if self._running:
            return
        if self._get_frame() is None:
            self._status_label.setText("No frame available — start the Main camera stream first.")
            return
        controller = FineCollimationController(
            FocusedStarAcquisition(roi_size=self._roi_size),
            get_frame=self._get_frame,
            optical_config=self._optical_config,
            sample_count=self._sample_count,
            target_mode=self._target_mode,
            guide_reacquirer=self._guide_reacquirer,
        )
        started = self._runner.submit(controller)
        if not started:
            return  # a run is already in flight
        self._running = True
        self._status_label.setText("Measuring fine collimation…")
        self._cancel_button.setEnabled(True)
        self._run_button.setEnabled(False)
        self._poll_timer.start()

    def _on_cancel_clicked(self) -> None:
        self._runner.cancel()

    def _poll_run(self) -> None:
        outcome = self._runner.take_latest()
        if outcome is None:
            return
        self._poll_timer.stop()
        self._running = False
        self._run_button.setEnabled(True)
        self._cancel_button.setEnabled(False)
        self.show_outcome(outcome.outcome)

    def show_outcome(self, outcome: FineCollimationOutcome) -> None:
        """Render one completed run's outcome. Public -- called by
        `_poll_run` when driven live, and directly by tests to exercise
        the rendering logic without the runner/threading."""
        self._last_outcome = outcome
        if outcome.status == "failed" or outcome.result is None:
            self._set_style(_WARNING_STYLE, collimated=False)
            self._status_label.setText(f"{self._prefix()}: failed — {outcome.reason}")
            return
        prefix = self._prefix(outcome.result.target_mode)

        result = outcome.result
        profile_result = result.profile_result
        symmetry_result = result.symmetry_result

        center = profile_result.center
        direction = symmetry_result.asymmetry_direction_deg
        magnitude = symmetry_result.asymmetry_magnitude
        if result.stack_result.stacked is not None:
            self._star_view.set_display(
                result.stack_result.stacked, center=center, direction_deg=direction,
                magnitude=magnitude,
            )

        if profile_result.profile is not None:
            self._profile_widget.set_profile(
                profile_result.profile.radii_px, profile_result.profile.normalized_intensity,
                measured_ring_radius_px=profile_result.first_ring_radius_px,
                expected_ring_radius_px=result.reference_result.expected_first_null_radius_px,
            )
        else:
            self._profile_widget.clear_profile()

        collimated = (
            symmetry_result.status is SymmetryStatus.FINE_COLLIMATED
            and profile_result.sufficient_sampling
        )

        if not profile_result.sufficient_sampling:
            self._set_style(_WARNING_STYLE, collimated=False)
            self._status_label.setText(
                f"{prefix}: insufficient sampling — {profile_result.reason}"
            )
        elif symmetry_result.status is SymmetryStatus.INVALID:
            self._set_style(_WARNING_STYLE, collimated=False)
            self._status_label.setText(f"{prefix}: invalid — {symmetry_result.reason}")
        elif symmetry_result.status is SymmetryStatus.LOW_CONFIDENCE:
            self._set_style(_WARNING_STYLE, collimated=False)
            self._status_label.setText(
                f"{prefix}: low confidence "
                f"({symmetry_result.confidence:.0%}) — not actionable"
            )
        elif (
            symmetry_result.status is SymmetryStatus.FINE_COLLIMATED
            and outcome.result.target_mode.finite_distance
        ):
            # Issue #39: a finite-distance source is not what the fine-
            # collimation optical model assumes -- never a final verdict.
            self._set_style(_WARNING_STYLE, collimated=False)
            self._status_label.setText(
                f"{prefix}: symmetric pattern (confidence {symmetry_result.confidence:.0%}) "
                "— finite-distance optics not validated, not a final collimation verdict"
            )
        elif symmetry_result.status is SymmetryStatus.FINE_COLLIMATED:
            self._set_style(_COLLIMATED_STYLE, collimated=collimated)
            self._status_label.setText(
                f"{prefix}: collimated (confidence {symmetry_result.confidence:.0%})"
            )
        else:  # ASYMMETRIC
            self._set_style(_WARNING_STYLE, collimated=False)
            direction_text = f"{direction:.0f}°" if direction is not None else "unknown"
            magnitude_text = f"{magnitude:.2f}" if magnitude is not None else "unknown"
            self._status_label.setText(
                f"{prefix}: asymmetric — error {magnitude_text} @ {direction_text} "
                f"(confidence {symmetry_result.confidence:.0%})"
            )

    @property
    def target_mode(self) -> CollimationTargetMode:
        return self._target_mode

    def set_target_mode(self, mode: CollimationTargetMode) -> None:
        index = self._target_mode_combo.findData(mode)
        if index >= 0 and index != self._target_mode_combo.currentIndex():
            self._target_mode_combo.setCurrentIndex(index)  # -> _on_target_mode_selected

    def _on_target_mode_selected(self) -> None:
        mode = self._target_mode_combo.currentData()
        if isinstance(mode, CollimationTargetMode) and mode is not self._target_mode:
            self._target_mode = mode
            self.target_mode_changed.emit(mode)

    def _prefix(self, mode: CollimationTargetMode | None = None) -> str:
        mode = mode if mode is not None else self._target_mode
        if mode is CollimationTargetMode.ARTIFICIAL_STAR:
            return "FINE COLLIMATION (artificial star)"
        return "FINE COLLIMATION"

    def _set_style(self, style: str, *, collimated: bool) -> None:
        self.is_collimated_style_active = collimated
        self._status_label.setStyleSheet(style)

    def diagnostic_context(self) -> dict[str, object]:
        context: dict[str, object] = {
            "running": self._running,
            "collimated": self.is_collimated_style_active,
            "target_mode": self._target_mode.value,
        }
        outcome = self._last_outcome
        if outcome is not None:
            context["last_status"] = outcome.status
            context["last_reason"] = outcome.reason
            context["last_reacquisition_log"] = list(outcome.reacquisition_log)
            if outcome.result is not None:
                context["last_target_mode"] = outcome.result.target_mode.value
                context["last_symmetry_status"] = outcome.result.symmetry_result.status.value
                context["last_confidence"] = outcome.result.symmetry_result.confidence
        return context

    def stop(self) -> None:
        self._poll_timer.stop()
        if self._running:
            self._runner.cancel()
