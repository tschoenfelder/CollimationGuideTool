"""The rough-collimation overlay: both rings and, clearly, which way the hole is off.

Operator (2026-11-27): the numbers are "usable only, when you overlay the rings and clearly
show, which direction the ... mismatch is". Everything is drawn in the target pixmap's own
pixels: lines drawn into a 3840 px frame vanish once it is scaled to the screen, and a 20 px
offset is only a few screen pixels -- so the arrow has a fixed, readable length and shows the
direction, while the label says how far.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen, QPixmap, QPolygonF

from collimation_tool.domain.collimation_measurement import CircleEllipseFit, DonutMeasurement
from collimation_tool.domain.screw_guidance import Screw, ScrewHint

OUTER_COLOR = QColor(235, 235, 235)
INNER_COLOR = QColor(190, 190, 190)
#: The mismatch arrow (operator, 2026-11-27: yellow; red, green and blue are the screws).
ARROW_COLOR = QColor(255, 220, 0)
SCREW_COLORS: dict[Screw, QColor] = {
    Screw.RED: QColor(255, 60, 60),
    Screw.GREEN: QColor(60, 230, 90),
    Screw.BLUE: QColor(80, 140, 255),
}
CENTRED_COLOR = QColor(80, 255, 120)
#: A hole this close to the ring centre (share of the outer radius) reads as centred.
CENTRED_SHARE = 0.01
#: The arrow's length: this share of the outer radius, at least `_MIN_ARROW_PX` (at size 1).
_ARROW_RADIUS_SHARE = 0.6
_MIN_ARROW_PX = 60.0
#: A screw arrow at full strength (1/8 turn): this share of the outer radius, at least
#: `_MIN_SCREW_ARROW_PX` (at size 1).
_SCREW_RADIUS_SHARE = 0.8
_MIN_SCREW_ARROW_PX = 90.0  # so a quarter-length arrow is still visible
#: On a narrower image (at size 1) the label would cover the rings, so it is left out.
_MIN_LABEL_WIDTH_PX = 300.0
_DIRECTIONS = (
    "up", "up-right", "right", "down-right", "down", "down-left", "left", "up-left",
)


@dataclass(frozen=True)
class Mismatch:
    """How far, and which way on screen, the hole sits from the outer ring's centre."""

    offset_px: float
    share_of_radius: float
    #: "left", "up-right", ... as seen in the image.
    direction: str
    #: 12 = up, 3 = right.
    clock: int

    @property
    def centred(self) -> bool:
        return self.share_of_radius < CENTRED_SHARE

    def text(self) -> str:
        share = f"{self.share_of_radius * 100:.1f} % of the ring radius"
        if self.centred:
            return f"Hole centred: {self.offset_px:.0f} px off ({share})"
        return (
            f"Hole is {self.offset_px:.0f} px {self.direction} of the ring centre "
            f"({self.clock} o'clock), {share}"
        )


def describe_mismatch(measurement: DonutMeasurement) -> Mismatch:
    dx, dy = measurement.error_x_px, measurement.error_y_px
    radius = max(measurement.outer_ring.radius_x, measurement.outer_ring.radius_y, 1.0)
    offset = math.hypot(dx, dy)
    bearing = math.degrees(math.atan2(dx, -dy)) % 360.0  # clockwise from up
    return Mismatch(
        offset_px=offset,
        share_of_radius=offset / radius,
        direction=_DIRECTIONS[round(bearing / 45.0) % 8],
        clock=round(bearing / 30.0) % 12 or 12,
    )


def offset_share(measurement: DonutMeasurement) -> tuple[float, float]:
    """The hole's offset from the ring centre as shares of the outer radius."""
    radius = max(measurement.outer_ring.radius_x, measurement.outer_ring.radius_y, 1.0)
    return (measurement.error_x_px / radius, measurement.error_y_px / radius)


def _arrow(
    painter: QPainter,
    start: QPointF,
    unit: tuple[float, float],
    length: float,
    color: QColor,
    width: float,
    head: float,
) -> QPointF:
    """Draw an arrow from `start` along `unit`; returns its tip."""
    ux, uy = unit
    head = min(head, length)
    tip = start + QPointF(ux * length, uy * length)
    base = tip - QPointF(ux * head, uy * head)
    side = QPointF(-uy * head * 0.6, ux * head * 0.6)
    painter.setPen(QPen(color, width))
    painter.drawLine(start, base)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPolygon(QPolygonF([tip, base + side, base - side]))
    return tip


def _ring(
    painter: QPainter, ring: CircleEllipseFit, color: QColor, scale: float, size: float
) -> None:
    painter.setPen(QPen(color, 2.0 * size))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    centre = QPointF(ring.center_x * scale, ring.center_y * scale)
    painter.drawEllipse(centre, ring.radius_x * scale, ring.radius_y * scale)
    cross = 8.0 * size
    painter.drawLine(centre - QPointF(cross, 0), centre + QPointF(cross, 0))
    painter.drawLine(centre - QPointF(0, cross), centre + QPointF(0, cross))


def _label(
    painter: QPainter, text: str, color: QColor, width: float, size: float, top: float
) -> float:
    """Draw one boxed line of text at height `top`; returns the box height."""
    font = QFont(painter.font())
    font.setBold(True)
    font.setPixelSize(max(1, round(16 * size)))
    metrics = QFontMetricsF(font)
    margin = 6.0 * size
    box = metrics.boundingRect(
        QRectF(0, 0, width - 2 * margin, 1.0e4), int(Qt.TextFlag.TextWordWrap), text
    )
    background = QRectF(0, top, box.width() + 2 * margin, box.height() + 2 * margin)
    painter.fillRect(background, QColor(0, 0, 0, 190))
    painter.setFont(font)
    painter.setPen(color)
    painter.drawText(
        QRectF(margin, top + margin, box.width() + 1, box.height() + 1),
        int(Qt.TextFlag.TextWordWrap),
        text,
    )
    return background.height()


def draw_collimation_overlay(
    pixmap: QPixmap,
    measurement: DonutMeasurement,
    *,
    scale: float,
    size: float = 1.0,
    show_mismatch: bool = True,
    screw_hints: Sequence[ScrewHint] = (),
) -> None:
    """Draw onto `pixmap`, which shows the frame scaled by `scale`; `size` scales line widths
    and text (1 = screen pixels). `show_mismatch`: also the direction arrow and the label,
    and an arrow per entry of `screw_hints` (length = how much to turn that screw)."""
    if pixmap.isNull():
        return
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        _ring(painter, measurement.outer_ring, OUTER_COLOR, scale, size)
        _ring(painter, measurement.inner_hole, INNER_COLOR, scale, size)
        if not show_mismatch:
            return
        mismatch = describe_mismatch(measurement)
        outer = measurement.outer_ring
        centre = QPointF(outer.center_x * scale, outer.center_y * scale)
        hints = () if mismatch.centred else screw_hints
        for hint in hints:
            if hint.strength <= 0:
                continue
            longest = max(
                _SCREW_RADIUS_SHARE * outer.radius_x * scale, _MIN_SCREW_ARROW_PX * size
            )
            _arrow(
                painter,
                centre,
                hint.direction,
                hint.strength * longest,
                SCREW_COLORS[hint.screw],
                (7.0 if hint.best else 4.0) * size,
                (20.0 if hint.best else 14.0) * size,
            )
        if not mismatch.centred:
            length = max(_ARROW_RADIUS_SHARE * outer.radius_x * scale, _MIN_ARROW_PX * size)
            unit = (
                measurement.error_x_px / mismatch.offset_px,
                measurement.error_y_px / mismatch.offset_px,
            )
            _arrow(painter, centre, unit, length, ARROW_COLOR, 5.0 * size, 16.0 * size)
        if pixmap.width() < _MIN_LABEL_WIDTH_PX * size:
            return
        lines = [(mismatch.text(), CENTRED_COLOR if mismatch.centred else ARROW_COLOR)]
        lines += [(hint.text(), SCREW_COLORS[hint.screw]) for hint in hints]
        top = 0.0
        for text, color in lines:
            top += _label(painter, text, color, float(pixmap.width()), size, top)
    finally:
        painter.end()
