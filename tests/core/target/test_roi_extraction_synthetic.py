"""Issue #15 AC 1.1 (S7): ROI extraction end to end on a synthetic frame.

`test_roi.py` checks `compute_roi_bounds` for a GIVEN center and `test_detector.py` /
`test_roi_selector.py` check detection and selection on their own. AC 1.1 is the chain:
a frame with one star at a known location yields an ROI centered on that star, with the
requested dimensions. These tests run exactly that chain (detect -> select -> bounds ->
crop) on synthetic frames, pure functions only."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.roi import compute_roi_bounds, crop_to_roi
from astrotool_core.target.roi_selector import select_target
from astrotool_core.testing.frame_factory import single_star_image

_SHAPE = (480, 640)  # (height, width)
_SIZE = (64, 48)  # (width, height)


@pytest.mark.parametrize(
    ("star_x", "star_y"),
    [(320.0, 240.0), (101.3, 377.8), (560.6, 61.2)],
    ids=["center", "lower-left", "upper-right"],
)
def test_one_star_at_a_known_location_yields_a_centered_roi_of_the_requested_size(
    star_x: float, star_y: float
) -> None:
    frame = single_star_image(_SHAPE, x=star_x, y=star_y, peak=5000.0, sigma=2.0)

    target = select_target(detect_sources(frame))

    assert target is not None
    assert (target.x, target.y) == pytest.approx((star_x, star_y), abs=0.5)
    roi = compute_roi_bounds(_SHAPE, center=(target.x, target.y), size=_SIZE)
    assert (roi.width, roi.height) == _SIZE
    # the window's geometric center is on the star (integer placement: <= 1 px)
    assert roi.x + roi.width / 2.0 == pytest.approx(star_x, abs=1.0)
    assert roi.y + roi.height / 2.0 == pytest.approx(star_y, abs=1.0)


def test_the_cropped_roi_contains_the_star_at_its_center() -> None:
    frame = single_star_image(_SHAPE, x=211.4, y=133.7, peak=5000.0, sigma=2.0)
    target = select_target(detect_sources(frame))
    assert target is not None

    crop = crop_to_roi(frame, compute_roi_bounds(_SHAPE, center=(target.x, target.y), size=_SIZE))

    assert crop.shape == (_SIZE[1], _SIZE[0])
    peak_y, peak_x = np.unravel_index(int(np.argmax(crop)), crop.shape)
    assert abs(peak_x - _SIZE[0] / 2.0) <= 1.5
    assert abs(peak_y - _SIZE[1] / 2.0) <= 1.5
