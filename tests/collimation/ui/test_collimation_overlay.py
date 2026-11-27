"""Operator (2026-11-27): the rough-collimation numbers are usable only with the rings drawn
over the image and the mismatch direction clearly shown."""

from __future__ import annotations

import math

import numpy as np
import pytest
from collimation_tool.domain.collimation_measurement import CircleEllipseFit, DonutMeasurement
from collimation_tool.ui.collimation_overlay import (
    ARROW_COLOR,
    OUTER_COLOR,
    describe_mismatch,
)
from collimation_tool.ui.live_view import LiveViewLabel
from PySide6.QtGui import QColor, QImage


def _measurement(dx: float, dy: float) -> DonutMeasurement:
    """The field case (bundle 10e89885): a 700 px ring on a 3840x2160 frame."""
    outer = CircleEllipseFit(2473.0, 901.0, 701.0, 701.0, 0.0, 0.9)
    inner = CircleEllipseFit(2473.0 + dx, 901.0 + dy, 517.0, 517.0, 0.0, 0.9)
    magnitude = math.hypot(dx, dy)
    angle = math.degrees(math.atan2(dy, dx))
    return DonutMeasurement(outer, inner, dx, dy, magnitude, angle, 0.9)


@pytest.mark.parametrize(
    ("dx", "dy", "direction", "clock"),
    [
        (-19.3, 3.2, "left", 9),
        (20.0, 0.0, "right", 3),
        (0.0, -20.0, "up", 12),
        (0.0, 20.0, "down", 6),
        (15.0, -15.0, "up-right", 2),
    ],
)
def test_the_direction_is_named_as_seen_on_screen(
    dx: float, dy: float, direction: str, clock: int
) -> None:
    mismatch = describe_mismatch(_measurement(dx, dy))

    assert (mismatch.direction, mismatch.clock) == (direction, clock)
    assert f"px {direction} of the ring centre" in mismatch.text()


def test_a_hole_within_one_percent_reads_as_centred() -> None:
    mismatch = describe_mismatch(_measurement(4.0, 0.0))

    assert mismatch.centred
    assert mismatch.text().startswith("Hole centred")


def _close(image: QImage, x: int, y: int, color: QColor) -> bool:
    """Whether a pixel near (x, y) has about `color`."""
    def near(pixel: QColor) -> bool:
        return (
            abs(pixel.red() - color.red()) <= 40
            and abs(pixel.green() - color.green()) <= 40
            and abs(pixel.blue() - color.blue()) <= 40
        )

    return any(near(image.pixelColor(x + i, y + j)) for i in range(-2, 3) for j in range(-2, 3))


def _shown(label: LiveViewLabel) -> tuple[QImage, float]:
    pixmap = label.pixmap()
    return pixmap.toImage(), pixmap.width() / 3840


def test_rings_and_a_readable_arrow_are_drawn_on_the_scaled_screen_image(qapp: object) -> None:
    label = LiveViewLabel()
    label.resize(640, 360)
    frame = np.zeros((2160, 3840), dtype=np.uint8)

    label.set_stretched_frame(frame, measurement=_measurement(-20.0, 0.0))

    image, scale = _shown(label)
    centre_x, centre_y = 2473.0 * scale, 901.0 * scale
    assert _close(image, round(centre_x + 701.0 * scale), round(centre_y), OUTER_COLOR)
    # A 20 px offset is 3 screen px here; the arrow still reaches far to the left.
    assert _close(image, round(centre_x - 50), round(centre_y), ARROW_COLOR)
    assert not _close(image, round(centre_x + 50), round(centre_y), ARROW_COLOR)


def test_the_guide_view_gets_no_mismatch_arrow(qapp: object) -> None:
    label = LiveViewLabel(show_mismatch=False)
    label.resize(640, 360)

    label.set_stretched_frame(
        np.zeros((2160, 3840), dtype=np.uint8), measurement=_measurement(-20.0, 0.0)
    )

    image, scale = _shown(label)
    assert not _close(image, round(2473.0 * scale - 50), round(901.0 * scale), ARROW_COLOR)


def test_the_diagnostic_image_keeps_the_overlay(qapp: object) -> None:
    label = LiveViewLabel()
    label.set_stretched_frame(
        np.zeros((2160, 3840), dtype=np.uint8), measurement=_measurement(-20.0, 0.0)
    )

    annotated = label.annotated_pixmap()

    assert annotated is not None
    assert annotated.width() == 3840
    assert _close(annotated.toImage(), 2473 - 300, 901, ARROW_COLOR)
