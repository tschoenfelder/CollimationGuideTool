"""Tests for Stage 3 quality selection and lucky stacking — issue #17
AC 3.1-3.3: score each registered frame, stack all-or-best-fraction,
keep a bounded rolling frame history."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.frame_registration import register_frames
from astrotool_core.target.point_source import PointSource
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.stacking import RollingFrameBuffer, score_frame_quality
from astrotool_core.testing.frame_factory import single_star_image

_SHAPE = (60, 80)
_BASE_X, _BASE_Y = 30.0, 25.0


def _star(*, peak: float = 3000.0, sigma: float = 2.0, background: float = 100.0) -> np.ndarray:
    return single_star_image(
        _SHAPE, x=_BASE_X, y=_BASE_Y, peak=peak, sigma=sigma, background=background
    )


def _measured_star(image: np.ndarray) -> PointSource:
    detection = detect_sources(image)
    star = select_target(detection)
    assert star is not None
    return star


class TestScoreFrameQuality:
    def test_sharp_star_scores_higher_than_blurred(self) -> None:
        sharp = _measured_star(_star(sigma=1.5))
        blurred = _measured_star(_star(sigma=6.0))

        sharp_score = score_frame_quality(sharp, background=100.0)
        blurred_score = score_frame_quality(blurred, background=100.0)

        assert sharp_score > blurred_score

    def test_no_measurable_width_falls_back_without_raising(self) -> None:
        star = PointSource(x=30.0, y=25.0, peak=3000.0, area=10, kind="normal_star")

        score = score_frame_quality(star, background=100.0)

        assert score > 0.0


class TestAC3_1StackRegisteredFrames:
    def test_stacked_peak_remains_centered_and_snr_improves(self) -> None:
        rng = np.random.default_rng(7)
        noise_sigma = 40.0
        frames = [_star() + rng.normal(0.0, noise_sigma, size=_SHAPE) for _ in range(9)]

        buffer = RollingFrameBuffer(capacity=20)
        for frame in frames:
            buffer.add(frame, _measured_star(frame))
        result = buffer.produce_stack()

        assert result.stacked is not None
        assert result.frame_count == 9
        detection = detect_sources(result.stacked)
        assert len(detection.sources) >= 1
        stacked_star = select_target(detection)
        assert stacked_star is not None
        assert stacked_star.x == pytest.approx(_BASE_X, abs=1.0)
        assert stacked_star.y == pytest.approx(_BASE_Y, abs=1.0)

        # Background-region noise should shrink noticeably once stacked
        # (conservative threshold, not an exact 1/sqrt(n) assertion, to
        # avoid flakiness from a single noise realization).
        corner = np.s_[0:10, 0:10]
        single_std = float(np.std(frames[0][corner]))
        stacked_std = float(np.std(result.stacked[corner]))
        assert stacked_std < single_std * 0.7


class TestAC3_2PoorFrameRejection:
    def test_blurred_frames_score_worse_than_sharp_frames(self) -> None:
        sharp_frames = [_star(sigma=1.5) for _ in range(3)]
        blurred_frames = [_star(sigma=6.0) for _ in range(3)]

        sharp_scores = [
            score_frame_quality(_measured_star(f), background=100.0) for f in sharp_frames
        ]
        blurred_scores = [
            score_frame_quality(_measured_star(f), background=100.0) for f in blurred_frames
        ]

        assert min(sharp_scores) > max(blurred_scores)

    def test_selected_stack_favors_better_ranked_frames(self) -> None:
        sharp_frames = [_star(sigma=1.5) for _ in range(3)]
        blurred_frames = [_star(sigma=6.0) for _ in range(3)]
        blurred_scores = [
            score_frame_quality(_measured_star(f), background=100.0) for f in blurred_frames
        ]

        buffer = RollingFrameBuffer(capacity=20)
        for frame in [*sharp_frames, *blurred_frames]:
            buffer.add(frame, _measured_star(frame))
        # 6 frames total, want exactly the 3 sharp ones kept.
        result = buffer.produce_stack(best_fraction=0.5)

        assert result.frame_count == 3
        assert min(result.quality_scores) > max(blurred_scores)


class TestAC3_3BoundedHistory:
    def test_history_never_exceeds_configured_capacity(self) -> None:
        buffer = RollingFrameBuffer(capacity=4)
        for i in range(10):
            frame = _star(peak=1000.0 + i * 100.0)
            buffer.add(frame, _measured_star(frame))
            assert len(buffer) <= 4

        assert len(buffer) == 4

    def test_only_the_most_recent_additions_survive_eviction(self) -> None:
        buffer = RollingFrameBuffer(capacity=3)
        frames = [_star(peak=1000.0 + i * 500.0) for i in range(7)]
        expected_scores = [
            score_frame_quality(_measured_star(frame), background=100.0) for frame in frames[-3:]
        ]
        for frame in frames:
            buffer.add(frame, _measured_star(frame))

        result = buffer.produce_stack()

        assert result.frame_count == 3
        assert list(result.quality_scores) == pytest.approx(expected_scores)


class TestStage2ToStage3Pipeline:
    def test_registered_frames_feed_directly_into_the_rolling_buffer(self) -> None:
        offsets = [(0.0, 0.0), (4.0, -3.0), (-5.0, 2.0)]
        frames = [
            single_star_image(_SHAPE, x=_BASE_X + dx, y=_BASE_Y + dy, peak=3000.0, sigma=2.0)
            for dx, dy in offsets
        ]

        registration = register_frames(frames)
        buffer = RollingFrameBuffer(capacity=10)
        for outcome in registration.outcomes:
            if outcome.usable and outcome.pixels is not None and outcome.star is not None:
                buffer.add(outcome.pixels, outcome.star)
        result = buffer.produce_stack()

        assert result.frame_count == 3
        assert result.stacked is not None
