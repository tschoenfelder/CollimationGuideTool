"""Issue #33 (artificial-star autofocus): the tracker keeps ONE target
through a focus sweep and reports an explicit reason for every sample it
cannot measure -- it never measures a different bright point instead."""

from __future__ import annotations

import numpy as np
import pytest
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
        assert result.size_px is not None
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
        assert result.size_px is None

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
        assert result.size_px is None

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

    def test_a_defocused_donut_is_measured_as_a_larger_star(self) -> None:
        # Issue #58 (review F2, changed from #33's "donut_like is rejected"): the half-flux
        # diameter measures a defocused donut too, so the sweep sees it shrink towards focus.
        tracker = StarTargetTracker()
        focused = tracker.acquire(_star())
        donut = donut_image(
            _SHAPE, outer_center=(80.0, 80.0), outer_radius=20.0,
            inner_center=(80.0, 80.0), inner_radius=8.0, peak=2000.0, background=100.0,
        )

        result = tracker.measure(donut)

        assert result.reason is None
        assert result.size_px is not None and focused.size_px is not None
        assert result.size_px > 3 * focused.size_px

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

        assert sharp.size_px is not None and blurry.size_px is not None
        assert sharp.size_px < blurry.size_px


def _over_exposed(
    shape: tuple[int, int], centres: list[tuple[float, float]], *, sigma: float = 16.0
) -> np.ndarray:
    """Issue #58: blobs far above full scale, clipped at 65520, on a noisy background --
    `detect_sources` fragments each into dozens of sources (as on diagnostic 4c5677d0)."""
    rng = np.random.default_rng(58)
    image = star_field_image(
        shape, [StarSpec(x=x, y=y, peak=2.0e6, sigma=sigma) for x, y in centres],
        background=1000.0,
    ).astype(np.float64)
    image += rng.normal(0.0, 30.0, shape)
    return np.clip(np.round(image), 0.0, 65520.0)


class TestSeveralOrOverExposedStars:
    """Issue #58: one blob is one candidate; with several, the one nearest the image centre
    (the green centre cross) is followed and the count is reported."""

    def test_a_single_over_exposed_blob_is_acquired_as_one_saturated_target(self) -> None:
        tracker = StarTargetTracker()

        result = tracker.acquire(_over_exposed((400, 400), [(210.0, 190.0)], sigma=25.0))

        assert result.reason == "saturated"  # not multiple_candidates
        assert tracker.target is not None
        assert abs(tracker.target[0] - 210.0) < 3.0 and abs(tracker.target[1] - 190.0) < 3.0
        assert tracker.candidate_count == 1

    def test_of_several_stars_the_one_nearest_the_centre_is_acquired(self) -> None:
        image = star_field_image(
            (240, 320),
            [StarSpec(x=60.0, y=50.0, peak=9000.0), StarSpec(x=175.0, y=110.0, peak=2000.0),
             StarSpec(x=270.0, y=200.0, peak=5000.0)],
            background=100.0,
        )
        tracker = StarTargetTracker()

        result = tracker.acquire(image)

        assert result.reason is None
        assert result.size_px is not None
        assert tracker.target is not None
        assert abs(tracker.target[0] - 175.0) < 1.0 and abs(tracker.target[1] - 110.0) < 1.0
        assert tracker.candidate_count == 3

    def test_of_several_over_exposed_blobs_the_one_nearest_the_centre_is_followed(
        self,
    ) -> None:
        centres = [(120.0, 110.0), (330.0, 120.0), (230.0, 230.0), (240.0, 330.0)]
        tracker = StarTargetTracker()

        result = tracker.acquire(_over_exposed((440, 440), centres))

        assert result.reason == "saturated"
        assert tracker.candidate_count == 4
        assert tracker.target is not None
        assert abs(tracker.target[0] - 230.0) < 3.0 and abs(tracker.target[1] - 230.0) < 3.0
        # The next frame, same scene: the same blob is measured, not a fragment of it.
        again = tracker.measure(_over_exposed((440, 440), centres))
        assert again.reason == "saturated"
        assert again.centroid is not None and abs(again.centroid[0] - 230.0) < 3.0

    def test_a_target_that_drops_below_full_scale_is_measured_as_one_star(self) -> None:
        tracker = StarTargetTracker()
        tracker.acquire(_over_exposed((440, 440), [(120.0, 110.0), (230.0, 230.0)]))
        lower = star_field_image(
            (440, 440),
            [StarSpec(x=120.0, y=110.0, peak=40000.0, sigma=3.0),
             StarSpec(x=231.0, y=229.0, peak=20000.0, sigma=3.0)],
            background=1000.0,
        )

        result = tracker.measure(lower)

        assert result.reason is None
        assert result.size_px is not None
        assert result.centroid is not None and abs(result.centroid[0] - 231.0) < 1.0

    @pytest.mark.parametrize("sigma", [12.0, 16.0, 20.0])
    def test_a_big_clipped_star_is_acquired_as_saturated_not_missed(self, sigma: float) -> None:
        # Review F1: detect_sources drops this blob (component mode, > 5000 px) -> no_star,
        # and the exposure step-down never started.
        tracker = StarTargetTracker()

        result = tracker.acquire(_over_exposed((1080, 1440), [(700.0, 520.0)], sigma=sigma))

        assert result.reason == "saturated"
        assert tracker.target is not None
        assert abs(tracker.target[0] - 700.0) < 2.0 and abs(tracker.target[1] - 520.0) < 2.0

    def test_two_candidates_equally_near_the_centre_are_not_guessed(self) -> None:
        image = star_field_image(
            _SHAPE,
            [StarSpec(x=40.0, y=79.5, peak=3000.0), StarSpec(x=119.0, y=79.5, peak=3000.0)],
            background=100.0,
        )
        tracker = StarTargetTracker()

        result = tracker.acquire(image)

        assert result.reason == "multiple_candidates"
        assert tracker.target is None
        assert tracker.candidate_count == 2


class TestReviewRound2:
    def test_a_warm_pixel_at_the_centre_is_not_followed_instead_of_the_star(self) -> None:
        # Review N1: the warm pixel was the candidate nearest the centre (HFD 0).
        frame = _over_exposed((400, 400), [(300.0, 120.0)], sigma=3.0)
        frame = np.minimum(frame, 20000.0)  # an unclipped star
        frame[200, 200] = 8000.0  # warm pixel exactly at the centre
        tracker = StarTargetTracker()

        result = tracker.acquire(frame)

        assert result.reason is None
        assert tracker.target is not None
        assert abs(tracker.target[0] - 300.0) < 1.0 and abs(tracker.target[1] - 120.0) < 1.0
        assert tracker.candidate_count == 1

    def test_acquire_and_measure_give_the_same_size_beside_a_bright_distant_star(self) -> None:
        # Review N2: start_value (acquire, whole frame) and the later samples (measure, crop)
        # must be on one scale.
        rng = np.random.default_rng(5)
        frame = star_field_image(
            (600, 800),
            [StarSpec(x=650.0, y=150.0, peak=2.0e6, sigma=5.0),
             StarSpec(x=400.0, y=300.0, peak=600.0, sigma=12.0)],
            background=1000.0,
        ).astype(np.float64)
        frame = np.clip(np.round(frame + rng.normal(0.0, 30.0, frame.shape)), 0.0, 65520.0)
        tracker = StarTargetTracker()

        acquired = tracker.acquire(frame)
        measured = tracker.measure(frame)

        assert acquired.size_px is not None and measured.size_px is not None
        assert measured.size_px == pytest.approx(acquired.size_px, rel=1e-9)

    def test_a_known_full_scale_is_kept_and_an_inferred_one_never_shrinks(self) -> None:
        # Review N3: the controller hands the run's full scale to the next attempt's tracker.
        dim = np.minimum(_over_exposed((300, 300), [(150.0, 150.0)], sigma=3.0), 4060.0)

        assert StarTargetTracker().acquire(dim).reason == "saturated"  # inferred 4095
        assert StarTargetTracker(full_scale=65535.0).acquire(dim).reason is None
        assert StarTargetTracker(full_scale_floor=65535.0).acquire(dim).reason is None


def test_the_tracker_needs_no_plate_solver() -> None:
    import astrotool_core.focus.star_focus_metric as module

    assert not hasattr(module, "AstapSolver")
    assert not hasattr(module, "AstapCliSolver")
