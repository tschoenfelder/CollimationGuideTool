"""LiveViewLabel — renders one guide frame plus the drift-vector overlay.

New (Stage 7): no analog in smart_telescope (its UI is browser/JS, not
PySide6). The percentile-stretch helper mirrors
`collimation_tool.ui.live_view`'s, but is kept independently per this
project's guide_tool/collimation_tool dependency-independence rule
(see CONTRIBUTING.md) rather than imported from there.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel

from guide_tool.domain.guide_error import GuideError

_TARGET_COLOR = QColor(90, 230, 130)
_CENTROID_COLOR = QColor(255, 210, 60)
_DRIFT_COLOR = QColor(255, 90, 90)
_LOST_COLOR = QColor(255, 90, 90)


def _stretch_to_uint8(mono: np.ndarray, *, low: float = 1.0, high: float = 99.5) -> np.ndarray:
    """Percentile-stretch a mono float array to a displayable uint8 copy."""
    lo = float(np.percentile(mono, low))
    hi = float(np.percentile(mono, high))
    if hi <= lo:
        hi = lo + 1.0
    stretched = np.clip((mono - lo) / (hi - lo), 0.0, 1.0)
    result: np.ndarray = (stretched * 255.0).astype(np.uint8)
    return result


def _cross(painter: QPainter, x: float, y: float, size: float) -> None:
    painter.drawLine(int(x - size), int(y), int(x + size), int(y))
    painter.drawLine(int(x), int(y - size), int(x), int(y + size))


class LiveViewLabel(QLabel):
    """Displays the latest guide-camera frame with the current guide error overlay."""

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(320, 240)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setScaledContents(True)
        self.setStyleSheet("background-color: black;")
        self.setText("No frame yet")

    def set_frame(self, mono: np.ndarray, *, error: GuideError | None) -> None:
        gray = _stretch_to_uint8(mono)
        height, width = gray.shape
        image = QImage(
            gray.tobytes(), width, height, width, QImage.Format.Format_Grayscale8
        ).copy()
        pixmap = QPixmap.fromImage(image)

        painter = QPainter(pixmap)
        try:
            if error is None:
                pass
            elif not error.accepted:
                painter.setPen(QPen(_LOST_COLOR, 2))
                painter.drawText(10, 20, f"NO LOCK ({error.rejected_reason})")
            else:
                if error.target_x is not None and error.target_y is not None:
                    painter.setPen(QPen(_TARGET_COLOR, 2))
                    _cross(painter, error.target_x, error.target_y, 10)
                if error.centroid_x is not None and error.centroid_y is not None:
                    painter.setPen(QPen(_CENTROID_COLOR, 2))
                    _cross(painter, error.centroid_x, error.centroid_y, 6)
                if (
                    error.target_x is not None
                    and error.target_y is not None
                    and error.centroid_x is not None
                    and error.centroid_y is not None
                ):
                    painter.setPen(QPen(_DRIFT_COLOR, 2))
                    painter.drawLine(
                        int(error.target_x),
                        int(error.target_y),
                        int(error.centroid_x),
                        int(error.centroid_y),
                    )
        finally:
            painter.end()

        self.setPixmap(pixmap)
