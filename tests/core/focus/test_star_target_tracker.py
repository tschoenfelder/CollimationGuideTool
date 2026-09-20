"""Issue #33 (artificial-star autofocus): the tracker keeps ONE target
through a focus sweep and reports an explicit reason for every sample it
cannot measure -- it never measures a different bright point instead."""

from __future__ import annotations

import numpy as np
from astrotool_core.focus.star_focus_metric import StarTargetTracker
from astrotool_core.testing.frame_factory import (
    StarSpec,
    donut_image,
    single_star_image,
    star_field_image,
)

_SHAPE = (160, 160)


def _star(
    x: float = 80.0, y: float = 80.0, *, sigma: float = 2.5, peak: float = 3000.0
) -> np.ndarray:
    return single_star_image(_SHAPE, x=x, y=y, peak=peak, sigma=sigma, background=100.0)


class TestAcquire:
    def test_exactly_one_star_is_acquired_as_the_target(self) -> None:
        tracker = StarTargetTracker()

        result = tracker.acquire(_star())

        assert result.reason is None
        assert result.fwhm_px is not None
        assert tracker.target is not None
        assert abs(tracker.target[0] - 80.0) < 1.0

    def test_two_candidates_are_never_silently_resolved(self) -> None:
        image = star_field_image(
            _SHAPE, [StarSpec(x=50.0, y=80.0, peak=3000.0), StarSpec(x=110.0, y=80.0, peak=2000.0)],
            background=100.0,
        )
        tracker = StarTargetTracker()

        result = tracker.acquire(image)

        assert result.reason == "multiple_candidates"
        assert tracker.target is None

    def test_no_star_reports_no_star(self) -> None:
        tracker = StarTargetTracker()

        result = tracker.acquire(np.full(_SHAPE, 100.0))

        assert result.reason == "no_star"

    def test_a_saturated_star_is_rejected_with_that_reason(self) -> None:
        tracker = StarTargetTracker()

        result = tracker.acquire(_star(peak=70000.0))

        assert result.reason == "saturated"
        assert result.fwhm_px is None

    def test_an_edge_clipped_star_is_rejected_with_that_reason(self) -> None:
        tracker = StarTargetTracker(edge_margin_px=16)

        result = tracker.acquire(_star(x=100.0, y=155.0))

        assert result.reason in ("edge_clipped", "no_star")


class TestMeasureFollowsTheSameTarget:
    def test_a_stray_bright_point_is_never_measured_instead(self) -> None:
        tracker = StarTargetTracker()
        tracker.acquire(_star())
        # The real target is gone at this focus position; only a distant
        # bright point remains.
        stray = _star(x=140.0, y=30.0, sigma=1.5, peak=9000.0)

        result = tracker.measure(stray)

        assert result.reason == "not_found_at_tracked_position"
        assert result.fwhm_px is None

    def test_the_target_is_still_measured_beside_a_brighter_distractor(self) -> None:
        tracker = StarTargetTracker()
        tracker.acquire(_star())
        image = star_field_image(
            _SHAPE,
            [StarSpec(x=80.0, y=80.0, peak=2000.0, sigma=3.0),
             StarSpec(x=140.0, y=30.0, peak=9000.0, sigma=1.5)],
            background=100.0,
        )

        result = tracker.measure(image)

        assert result.reason is None
        assert result.centroid is not None
        assert abs(result.centroid[0] - 80.0) < 2.0

    def test_a_small_centroid_shift_is_followed(self) -> None:
        tracker = StarTargetTracker(max_shift_px=40.0)
        tracker.acquire(_star())

        first = tracker.measure(_star(x=90.0, y=84.0))
        second = tracker.measure(_star(x=100.0, y=88.0))

        assert first.reason is None and second.reason is None

    def test_a_defocused_donut_is_rejected_with_that_reason(self) -> None:
        tracker = StarTargetTracker()
        tracker.acquire(_star())
        donut = donut_image(
            _SHAPE, outer_center=(80.0, 80.0), outer_radius=20.0,
            inner_center=(80.0, 80.0), inner_radius=8.0, peak=2000.0, background=100.0,
        )

        result = tracker.measure(donut)

        assert result.reason == "donut_like"

    def test_saturation_at_a_later_position_is_reported_as_saturated(self) -> None:
        tracker = StarTargetTracker()
        tracker.acquire(_star())

        result = tracker.measure(_star(sigma=1.5, peak=70000.0))

        assert result.reason == "saturated"

    def test_a_sharper_smaller_sigma_reports_a_smaller_fwhm(self) -> None:
        tracker = StarTargetTracker()
        tracker.acquire(_star(sigma=4.0))

        sharp = tracker.measure(_star(sigma=1.5))
        blurry = tracker.measure(_star(sigma=4.0))

        assert sharp.fwhm_px is not None and blurry.fwhm_px is not None
        assert sharp.fwhm_px < blurry.fwhm_px


def test_the_tracker_needs_no_plate_solver() -> None:
    import astrotool_core.focus.star_focus_metric as module

    assert not hasattr(module, "AstapSolver")
    assert not hasattr(module, "AstapCliSolver")
