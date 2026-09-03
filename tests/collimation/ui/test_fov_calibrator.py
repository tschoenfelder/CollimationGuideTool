import time
from collections.abc import Callable

import numpy as np
from astrotool_core.registration.optical_prior import OpticalPrior
from collimation_tool.ui.fov_calibrator import FovCalibrator, _auto_search_downsample


def _starfield(height: int, width: int, *, n_stars: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    image = np.full((height, width), 100.0, dtype=np.float64)
    ys, xs = np.meshgrid(np.arange(height), np.arange(width), indexing="ij")
    for _ in range(n_stars):
        cx, cy = rng.uniform(0, width), rng.uniform(0, height)
        peak = rng.uniform(500.0, 3000.0)
        sigma = rng.uniform(1.5, 3.0)
        image += peak * np.exp(-(((xs - cx) ** 2 + (ys - cy) ** 2) / (2 * sigma**2)))
    return image


def _priors() -> tuple[OpticalPrior, OpticalPrior]:
    prior_a = OpticalPrior(name="a", sensor_width_px=10, sensor_height_px=10,
                            pixel_scale_arcsec=1.0)
    prior_b = OpticalPrior(name="b", sensor_width_px=10, sensor_height_px=10,
                            pixel_scale_arcsec=1.0)
    return prior_a, prior_b


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestFovCalibrator:
    def test_submit_then_take_latest_returns_a_completed_outcome(self) -> None:
        guide = _starfield(60, 60, n_stars=15, seed=1)
        main = guide[15:45, 10:50].copy()
        calibrator = FovCalibrator()
        prior_a, prior_b = _priors()

        calibrator.submit(main, guide, prior_a=prior_a, prior_b=prior_b)
        assert _wait_for(lambda: not calibrator.is_busy)

        outcome = calibrator.take_latest()
        assert outcome is not None
        assert outcome.result.ok

    def test_take_latest_returns_none_when_nothing_has_completed_yet(self) -> None:
        assert FovCalibrator().take_latest() is None

    def test_take_latest_clears_the_outcome_so_it_is_returned_only_once(self) -> None:
        guide = _starfield(60, 60, n_stars=15, seed=2)
        main = guide[15:45, 10:50].copy()
        calibrator = FovCalibrator()
        prior_a, prior_b = _priors()
        calibrator.submit(main, guide, prior_a=prior_a, prior_b=prior_b)
        assert _wait_for(lambda: not calibrator.is_busy)

        assert calibrator.take_latest() is not None
        assert calibrator.take_latest() is None

    def test_a_submit_while_busy_is_a_no_op(self) -> None:
        guide = _starfield(60, 60, n_stars=15, seed=3)
        main = guide[15:45, 10:50].copy()
        calibrator = FovCalibrator()
        prior_a, prior_b = _priors()
        calibrator._busy = True  # simulate an in-flight calibration

        started = calibrator.submit(main, guide, prior_a=prior_a, prior_b=prior_b)
        assert started is False
        assert calibrator.take_latest() is None  # nothing was actually started

    def test_no_confident_match_still_produces_an_outcome(self) -> None:
        guide = _starfield(60, 60, n_stars=15, seed=4)
        rng = np.random.default_rng(99)
        unrelated = rng.normal(500.0, 50.0, size=(20, 25))
        calibrator = FovCalibrator()
        prior_a, prior_b = _priors()

        calibrator.submit(unrelated, guide, prior_a=prior_a, prior_b=prior_b)
        assert _wait_for(lambda: not calibrator.is_busy)

        outcome = calibrator.take_latest()
        assert outcome is not None
        assert not outcome.result.ok  # ran to completion, just no match

    def test_bad_input_is_reported_as_no_match_not_a_crash(self) -> None:
        guide = _starfield(30, 30, n_stars=5, seed=5)
        too_big = np.full((80, 80), 500.0)  # can't fit inside guide at any scale
        calibrator = FovCalibrator()
        prior_a, prior_b = _priors()

        calibrator.submit(too_big, guide, prior_a=prior_a, prior_b=prior_b)
        assert _wait_for(lambda: not calibrator.is_busy)

        outcome = calibrator.take_latest()
        assert outcome is not None
        assert not outcome.result.ok


class TestLatestProgress:
    """See the real bug this was added for: "Calibration started but
    working without any status on progress"."""

    def test_progress_is_none_before_anything_is_submitted(self) -> None:
        assert FovCalibrator().latest_progress() is None

    def test_progress_advances_towards_completion_while_running(self) -> None:
        guide = _starfield(100, 100, n_stars=30, seed=6)
        main = guide[25:75, 20:80].copy()
        calibrator = FovCalibrator()
        prior_a, prior_b = _priors()

        calibrator.submit(main, guide, prior_a=prior_a, prior_b=prior_b)
        # Sample progress a few times while it's still running — each
        # sample should be a valid, non-decreasing (completed, total).
        samples: list[tuple[int, int]] = []
        deadline = time.monotonic() + 10.0
        while calibrator.is_busy and time.monotonic() < deadline:
            progress = calibrator.latest_progress()
            if progress is not None:
                samples.append(progress)
            time.sleep(0.01)
        assert _wait_for(lambda: not calibrator.is_busy)

        assert len(samples) > 0, "no progress was ever reported"
        completed_values = [c for c, _ in samples]
        assert completed_values == sorted(completed_values)  # never decreases
        totals = {total for _, total in samples}
        assert len(totals) == 1  # total stays constant through one run
        assert all(c <= next(iter(totals)) for c in completed_values)

    def test_progress_is_cleared_once_the_calibration_completes(self) -> None:
        guide = _starfield(60, 60, n_stars=15, seed=7)
        main = guide[15:45, 10:50].copy()
        calibrator = FovCalibrator()
        prior_a, prior_b = _priors()

        calibrator.submit(main, guide, prior_a=prior_a, prior_b=prior_b)
        assert _wait_for(lambda: not calibrator.is_busy)

        assert calibrator.latest_progress() is None


class TestAutoSearchDownsample:
    """See the real-world "very slow" report: production calibration
    should search at reduced resolution by default, but never so
    aggressively that a small/test-scale frame loses all its structure
    (a blanket factor once let unrelated noise score a false match on a
    20x25 test template shrunk to 5x6 pixels)."""

    def test_a_real_camera_sized_guide_frame_downsamples(self) -> None:
        guide = np.zeros((1080, 1920))
        assert _auto_search_downsample(guide) == 4

    def test_a_small_test_scale_guide_frame_is_not_downsampled(self) -> None:
        guide = np.zeros((60, 60))
        assert _auto_search_downsample(guide) == 1

    def test_exactly_at_the_target_dimension_is_not_downsampled(self) -> None:
        guide = np.zeros((480, 480))
        assert _auto_search_downsample(guide) == 1

    def test_submit_without_an_explicit_downsample_still_finds_a_real_match(self) -> None:
        # End-to-end: submit() with no search_downsample override must
        # actually use the auto-computed factor and still work.
        guide = _starfield(60, 60, n_stars=15, seed=42)
        main = guide[15:45, 10:50].copy()
        calibrator = FovCalibrator()
        prior_a, prior_b = _priors()

        calibrator.submit(main, guide, prior_a=prior_a, prior_b=prior_b)
        assert _wait_for(lambda: not calibrator.is_busy)

        outcome = calibrator.take_latest()
        assert outcome is not None
        assert outcome.result.ok
