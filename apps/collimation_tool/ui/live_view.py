"""LiveViewLabel — renders one captured frame plus the donut overlay.

New (Stage 7): no analog in smart_telescope (its UI is browser/JS, not
PySide6). The percentile-stretch formula mirrors
`collimation_tool.domain.collimation_measurement.auto_stretch`, but is
kept as an independent, UI-local helper rather than imported from
there: `auto_stretch` is analysis-facing (tuned for donut detection),
this is display-facing, and the two are free to diverge.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel

from collimation_tool.domain.collimation_measurement import CircleEllipseFit, DonutMeasurement

_OUTER_COLOR = QColor(80, 220, 255)
_INNER_COLOR = QColor(255, 210, 60)
_ERROR_COLOR = QColor(255, 90, 90)


def _stretch_to_uint8(mono: np.ndarray, *, low: float = 1.0, high: float = 99.5) -> np.ndarray:
    """Percentile-stretch a mono float array to a displayable uint8 copy."""
    lo = float(np.percentile(mono, low))
    hi = float(np.percentile(mono, high))
    if hi <= lo:
        hi = lo + 1.0
    stretched = np.clip((mono - lo) / (hi - lo), 0.0, 1.0)
    result: np.ndarray = (stretched * 255.0).astype(np.uint8)
    return result


def _draw_ring(painter: QPainter, ring: CircleEllipseFit, color: QColor) -> None:
    painter.setPen(QPen(color, 2))
    painter.drawEllipse(
        int(ring.center_x - ring.radius_x),
        int(ring.center_y - ring.radius_y),
        int(ring.radius_x * 2),
        int(ring.radius_y * 2),
    )
    cross = 6
    cx, cy = int(ring.center_x), int(ring.center_y)
    painter.drawLine(cx - cross, cy, cx + cross, cy)
    painter.drawLine(cx, cy - cross, cx, cy + cross)


class LiveViewLabel(QLabel):
    """Displays the latest mono frame, optionally with a donut measurement overlay."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(320, 240)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setScaledContents(True)
        self.setStyleSheet("background-color: black;")
        self.setText("No frame yet")

    def set_frame(self, mono: np.ndarray, *, measurement: DonutMeasurement | None) -> None:
        gray = _stretch_to_uint8(mono)
        height, width = gray.shape
        image = QImage(
            gray.tobytes(), width, height, width, QImage.Format.Format_Grayscale8
        ).copy()
        pixmap = QPixmap.fromImage(image)

        if measurement is not None:
            painter = QPainter(pixmap)
            try:
                _draw_ring(painter, measurement.outer_ring, _OUTER_COLOR)
                _draw_ring(painter, measurement.inner_hole, _INNER_COLOR)
                painter.setPen(QPen(_ERROR_COLOR, 2))
                painter.drawLine(
                    int(measurement.outer_ring.center_x),
                    int(measurement.outer_ring.center_y),
                    int(measurement.inner_hole.center_x),
                    int(measurement.inner_hole.center_y),
                )
            finally:
                painter.end()

        self.setPixmap(pixmap)
