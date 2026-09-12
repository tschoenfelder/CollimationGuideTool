"""Tests for the fine-collimation ROI window — issue #15 AC 1.1: given a
selected star's position, the returned ROI is centered on it (within
tolerance) with the requested dimensions."""

from __future__ import annotations

import numpy as np
from astrotool_core.target.roi import compute_roi_bounds, crop_to_roi


class TestComputeRoiBounds:
    def test_roi_is_centered_on_the_requested_point(self) -> None:
        roi = compute_roi_bounds((480, 640), center=(320.0, 240.0), size=(64, 48))

        assert roi.width == 64
        assert roi.height == 48
        assert roi.center_x == 320.0
        assert roi.center_y == 240.0
        assert roi.x == 320 - 32
        assert roi.y == 240 - 24

    def test_roi_dimensions_always_match_the_requested_size_away_from_edges(self) -> None:
        roi = compute_roi_bounds((1000, 1000), center=(500.0, 500.0), size=(101, 51))
        assert roi.width == 101
        assert roi.height == 51

    def test_roi_near_the_top_left_corner_is_clamped_but_keeps_requested_size(self) -> None:
        roi = compute_roi_bounds((480, 640), center=(5.0, 3.0), size=(64, 48))

        assert roi.x == 0
        assert roi.y == 0
        assert roi.width == 64
        assert roi.height == 48

    def test_roi_near_the_bottom_right_corner_is_clamped_but_keeps_requested_size(self) -> None:
        roi = compute_roi_bounds((480, 640), center=(637.0, 478.0), size=(64, 48))

        assert roi.x == 640 - 64
        assert roi.y == 480 - 48
        assert roi.width == 64
        assert roi.height == 48

    def test_requested_size_larger_than_the_frame_is_clamped_to_the_frame(self) -> None:
        roi = compute_roi_bounds((100, 120), center=(50.0, 60.0), size=(500, 500))

        assert roi.x == 0
        assert roi.y == 0
        assert roi.width == 120
        assert roi.height == 100


class TestCropToRoi:
    def test_crop_matches_the_roi_bounds_and_content(self) -> None:
        frame = np.arange(480 * 640, dtype=np.float64).reshape(480, 640)
        roi = compute_roi_bounds((480, 640), center=(320.0, 240.0), size=(64, 48))

        cropped = crop_to_roi(frame, roi)

        assert cropped.shape == (48, 64)
        assert np.array_equal(
            cropped, frame[roi.y : roi.y + roi.height, roi.x : roi.x + roi.width]
        )
