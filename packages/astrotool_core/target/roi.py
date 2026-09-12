"""Fine-collimation analysis ROI — issue #15 AC 1.1: a movable analysis
window around a tracked target, not a fixed image location. Software
cropping only (native camera ROI is optional per the issue's own text,
not implemented here).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Roi:
    """`x`/`y` are the top-left corner, clamped to the frame's own
    bounds -- `center_x`/`center_y` are the requested center exactly as
    given (not re-derived from the clamped `x`/`y`), so a caller can
    always tell how far a clamped ROI actually sits from where it was
    asked to be centered."""

    x: int
    y: int
    width: int
    height: int
    center_x: float
    center_y: float


def compute_roi_bounds(
    frame_shape: tuple[int, int], center: tuple[float, float], size: tuple[int, int]
) -> Roi:
    """`frame_shape`/`size` are `(height, width)` and `(width, height)`
    respectively, matching `np.ndarray.shape`'s own row-major convention
    for the former and the natural `(width, height)` reading order for a
    requested window size. The returned window always has the requested
    `size` (clamped down to the frame's own size if larger), positioned
    to stay centered on `center` wherever the frame's own edges allow."""
    frame_height, frame_width = frame_shape
    center_x, center_y = center
    width = min(size[0], frame_width)
    height = min(size[1], frame_height)

    x = round(center_x - width / 2.0)
    y = round(center_y - height / 2.0)
    x = max(0, min(x, frame_width - width))
    y = max(0, min(y, frame_height - height))

    return Roi(x=x, y=y, width=width, height=height, center_x=center_x, center_y=center_y)


def crop_to_roi(frame: np.ndarray, roi: Roi) -> np.ndarray:
    return frame[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width]
