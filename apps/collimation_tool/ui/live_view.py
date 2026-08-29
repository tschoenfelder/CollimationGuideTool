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
from astrotool_core.frames import rgb_to_luma
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap, QPolygonF, QResizeEvent
from PySide6.QtWidgets import QLabel

from collimation_tool.domain.collimation_measurement import CircleEllipseFit, DonutMeasurement
from collimation_tool.ui.fov_overlay import FovOverlayRect

_OUTER_COLOR = QColor(80, 220, 255)
_INNER_COLOR = QColor(255, 210, 60)
_ERROR_COLOR = QColor(255, 90, 90)
_FOV_COLOR = QColor(255, 255, 0)


def _stretch_bounds(
    mono: np.ndarray, low: float, high: float, sample_stride: int
) -> tuple[float, float]:
    """Percentile bounds for stretching *mono*, estimated from a strided
    subsample rather than every pixel — measured at ~250ms on a real
    3840x2160 frame for the naive full-array `np.percentile` (a real
    contributor to the UI responsiveness issue this was found from). A
    stride of 4 (1/16 of the pixels in 2D) doesn't visibly change the
    stretch for a live-view display, which has no precision requirement.

    Returns (lo, hi) with hi > lo, unless the full-resolution frame is
    genuinely flat, in which case hi == lo and the caller decides how to
    render that (see stretch_to_uint8/stretch_rgb_to_uint8).
    """
    sample = mono[::sample_stride, ::sample_stride] if sample_stride > 1 else mono
    lo = float(np.percentile(sample, low))
    hi = float(np.percentile(sample, high))
    if hi > lo:
        return lo, hi
    # The *subsample* looks flat, but a small bright/dark feature can fall
    # entirely between subsampled pixels (e.g. a few-pixel-wide spot in a
    # 3840x2160 frame at stride 4) without the full frame actually being
    # uniform — re-check against the full array before trusting the
    # degenerate case, since that check is cheap (one min/max) compared to
    # the percentile call this stride exists to avoid.
    full_lo = float(mono.min())
    full_hi = float(mono.max())
    return full_lo, full_hi if full_hi > full_lo else full_lo


def stretch_to_uint8(
    mono: np.ndarray, *, low: float = 1.0, high: float = 99.5, sample_stride: int = 4
) -> np.ndarray:
    """Percentile-stretch a mono float array to a displayable uint8 copy."""
    lo, hi = _stretch_bounds(mono, low, high, sample_stride)
    if hi <= lo:
        # Genuinely uniform frame: either really empty (lo == 0) or — far
        # more likely, since real sensor read noise means an actually-dim
        # scene is never perfectly flat — saturated at whatever the
        # sensor's true ADC ceiling is (see auto_exposure's
        # saturation-fraction check for the companion fix to the same
        # real-hardware bug). The old `hi = lo + 1.0` fallback mapped
        # every pixel to (lo-lo)/(hi-lo) = 0 unconditionally — a solid
        # BLACK display for a *saturated* sensor, which read as "no
        # signal at all" and hid the real (overexposed) cause.
        fill = 255 if lo > 0.0 else 0
        return np.full(mono.shape, fill, dtype=np.uint8)
    stretched = np.clip((mono - lo) / (hi - lo), 0.0, 1.0)
    result: np.ndarray = (stretched * 255.0).astype(np.uint8)
    return result


def stretch_rgb_to_uint8(
    rgb: np.ndarray, *, low: float = 1.0, high: float = 99.5, sample_stride: int = 4
) -> np.ndarray:
    """Percentile-stretch a demosaiced (H, W, 3) RGB float array to a
    displayable uint8 copy, preserving color.

    The stretch range is computed from the frame's luma (the same plane
    FrameAnalyzer's analysis uses, and what stretch_to_uint8 does for a
    mono camera) and applied identically to all three channels, rather
    than stretching each channel independently — an independent
    per-channel stretch would shift color balance (effectively an
    auto-white-balance), which isn't the goal here: this only needs a
    color sensor's live view to actually show the color image it
    captured, rather than silently rendering it as luma-only mono (the
    bug this was added for — the analysis pipeline demosaics for donut
    detection, but the *display* was always built from that same mono
    plane, so a color camera's live view never showed color at all).
    """
    luma = rgb_to_luma(rgb)
    lo, hi = _stretch_bounds(luma, low, high, sample_stride)
    if hi <= lo:
        fill = 255 if lo > 0.0 else 0
        return np.full(rgb.shape, fill, dtype=np.uint8)
    stretched = np.clip((rgb - lo) / (hi - lo), 0.0, 1.0)
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
    """Displays the latest mono frame, optionally with a donut measurement overlay.

    Scales to fit the label's current size with X and Y scaled by the same
    factor (``Qt.KeepAspectRatio`` — no stretching/distortion), not a 1:1
    pixel mapping: the label is generally smaller than the sensor's native
    resolution, and this is deliberately independent of whatever scale any
    other view on screen is using (see CollimationTool's two-camera-panel
    layout, where the two panels aren't the same size).
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(320, 240)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background-color: black;")
        self.setText("No frame yet")
        self._base_pixmap: QPixmap | None = None

    def set_frame(
        self,
        mono: np.ndarray,
        *,
        measurement: DonutMeasurement | None,
        fov_rect: FovOverlayRect | None = None,
        fov_polygon: list[tuple[float, float]] | None = None,
    ) -> None:
        """Stretch and display *mono*. For a real (large) camera frame,
        prefer computing the stretch off the UI thread and calling
        `set_stretched_frame` instead — see `FrameAnalyzer`."""
        self.set_stretched_frame(
            stretch_to_uint8(mono),
            measurement=measurement,
            fov_rect=fov_rect,
            fov_polygon=fov_polygon,
        )

    def set_stretched_frame(
        self,
        stretched: np.ndarray,
        *,
        measurement: DonutMeasurement | None,
        fov_rect: FovOverlayRect | None = None,
        fov_polygon: list[tuple[float, float]] | None = None,
    ) -> None:
        """Display an already-stretched frame: *stretched* is either a 2D
        mono array (from `stretch_to_uint8`) or an (H, W, 3) RGB array
        (from `stretch_rgb_to_uint8`, for a color camera — see that
        function's docstring for why this matters).

        *fov_polygon* (native-pixel corners, e.g. from
        `fov_registration.registration_corners`) takes precedence over
        *fov_rect* when both are given — a real calibrated (and possibly
        rotated) match is more trustworthy than the config-only centered
        placeholder rectangle.
        """
        if stretched.ndim == 3:
            height, width, _ = stretched.shape
            rgb = np.ascontiguousarray(stretched)
            image = QImage(
                rgb.tobytes(), width, height, width * 3, QImage.Format.Format_RGB888
            ).copy()
        else:
            height, width = stretched.shape
            mono = np.ascontiguousarray(stretched)
            image = QImage(
                mono.tobytes(), width, height, width, QImage.Format.Format_Grayscale8
            ).copy()
        pixmap = QPixmap.fromImage(image)

        if measurement is not None or fov_rect is not None or fov_polygon is not None:
            painter = QPainter(pixmap)
            try:
                if measurement is not None:
                    _draw_ring(painter, measurement.outer_ring, _OUTER_COLOR)
                    _draw_ring(painter, measurement.inner_hole, _INNER_COLOR)
                    painter.setPen(QPen(_ERROR_COLOR, 2))
                    painter.drawLine(
                        int(measurement.outer_ring.center_x),
                        int(measurement.outer_ring.center_y),
                        int(measurement.inner_hole.center_x),
                        int(measurement.inner_hole.center_y),
                    )
                if fov_polygon is not None:
                    # A real calibrated match (see fov_registration) —
                    # drawn as an arbitrary quadrilateral so a genuine
                    # rotation actually shows as rotated, not just an
                    # axis-aligned box.
                    painter.setPen(QPen(_FOV_COLOR, 2))
                    painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in fov_polygon]))
                elif fov_rect is not None:
                    # Where the *other* (main) camera's field of view falls
                    # within this (guide) frame — see fov_overlay's docstring
                    # for what this rectangle does and doesn't account for
                    # (centered/unrotated placeholder, no measured alignment
                    # data exists yet).
                    painter.setPen(QPen(_FOV_COLOR, 2))
                    painter.drawRect(
                        int(fov_rect.x * width),
                        int(fov_rect.y * height),
                        int(fov_rect.width * width),
                        int(fov_rect.height * height),
                    )
            finally:
                painter.end()

        self._base_pixmap = pixmap
        self._update_scaled_pixmap()

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
