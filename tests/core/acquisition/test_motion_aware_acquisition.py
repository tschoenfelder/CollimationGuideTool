"""Tests for motion_aware_acquisition — issue #30's own required test
strategy: fake clock, fake camera-frame sequences, deterministic, no real
hardware. Deliberately reuses the *real* measure_translation_offset
underneath (via check_image_stability) against real synthetic textured
images with real np.roll shifts, not a mocked stability result -- the
point of this layer is exactly that a "stable" verdict has to be earned
from real image content, not merely asserted.
"""

from __future__ import annotations

import numpy as np
from astrotool_core.acquisition.motion_aware_acquisition import (
    CommandedMovementContext,
    MotionAwareStatus,
    acquire_verified_frame,
    acquire_verified_frames,
)
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
    StableFrameWaiter,
)


def _textured_image(shape: tuple[int, int], seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(loc=500.0, scale=80.0, size=shape)


class _FakeClock:
    def __init__(self, *, step_s: float = 0.05) -> None:
        self._now = 0.0
        self._step_s = step_s

    def now(self) -> float:
        return self._now

    def sleep(self, _seconds: float) -> None:
        self._now += self._step_s


def _sequence_waiter(clock: _FakeClock, frames: list[np.ndarray]) -> StableFrameWaiter:
    """A StableFrameWaiter delivering `frames` one at a time, in order,
    always reporting OK -- each call also advances the fake clock a
    little, standing in for real capture latency."""
    remaining = list(frames)

    def waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
        clock.sleep(0.02)
        frame = remaining.pop(0) if remaining else frames[-1]
        return FrameAcquisitionResult(
            FrameAcquisitionStatus.OK,
            DeliveredFrame(pixels=frame, captured_at_monotonic=clock.now(), exposure_seconds=0.0),
        )

    return waiter


def _failing_waiter(status: FrameAcquisitionStatus) -> StableFrameWaiter:
    def waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
        return FrameAcquisitionResult(status)

    return waiter


class TestAcquireVerifiedFrame:
    def test_an_already_stable_sequence_is_accepted(self) -> None:
        base = _textured_image((60, 60), seed=1)
        clock = _FakeClock()
        waiter = _sequence_waiter(clock, [base.copy(), base.copy(), base.copy()])

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=10.0, stability_tolerance_px=1.0,
            stability_sample_count=3, sleep=clock.sleep, now=clock.now,
        )

        assert result.status is MotionAwareStatus.OK
        assert result.ok
        assert result.frame is not None
        assert result.stability is not None and result.stability.stable

    def test_adaptive_wait_succeeds_once_the_image_actually_settles(self) -> None:
        """Issue #30's own "Mount becomes stable later" scenario: early
        frames disagree wildly (simulated vibration), later frames agree
        -- must wait past the early instability and accept once genuinely
        settled, not give up or accept early."""
        base = _textured_image((80, 80), seed=2)
        vibrating = [
            np.roll(base, shift=(15, 0), axis=(0, 1)),
            np.roll(base, shift=(0, -12), axis=(0, 1)),
            np.roll(base, shift=(9, 9), axis=(0, 1)),
        ]
        settled = [base.copy() for _ in range(4)]
        clock = _FakeClock()
        waiter = _sequence_waiter(clock, vibrating + settled)

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=10.0, stability_tolerance_px=1.0,
            stability_sample_count=3, stability_sample_interval_s=0.01,
            sleep=clock.sleep, now=clock.now,
        )

        assert result.status is MotionAwareStatus.OK
        assert result.stability is not None and result.stability.stable

    def test_wind_that_never_settles_within_the_deadline_is_image_not_stable(self) -> None:
        """Issue #30's own "External disturbance keeps image moving"
        scenario: every frame disagrees with its neighbors, forever --
        must report a stability failure, not accept anything, once the
        deadline (not a fixed settle constant) is reached."""
        base = _textured_image((80, 80), seed=3)
        rng = np.random.default_rng(99)

        def always_moving(clock: _FakeClock) -> StableFrameWaiter:
            def waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
                clock.sleep(0.02)
                dy, dx = rng.integers(5, 20), rng.integers(5, 20)
                shifted = np.roll(base, shift=(int(dy), int(dx)), axis=(0, 1))
                return FrameAcquisitionResult(
                    FrameAcquisitionStatus.OK,
                    DeliveredFrame(
                        pixels=shifted, captured_at_monotonic=clock.now(), exposure_seconds=0.0
                    ),
                )

            return waiter

        clock = _FakeClock(step_s=0.5)
        waiter = always_moving(clock)

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=3.0, stability_tolerance_px=1.0,
            stability_sample_count=3, stability_sample_interval_s=0.1,
            sleep=clock.sleep, now=clock.now,
        )

        assert result.status is MotionAwareStatus.IMAGE_NOT_STABLE
        assert not result.ok
        assert result.stability is not None
        assert not result.stability.stable

    def test_a_fixed_minimum_settle_alone_is_not_treated_as_stable(self) -> None:
        """Issue #30's own "Mount is still optically unstable after
        minimum settle time" scenario -- a caller granting only a short
        timeout that elapses before real stability is confirmed must NOT
        get an OK result just because *some* time passed."""
        base = _textured_image((60, 60), seed=4)
        clock = _FakeClock(step_s=0.3)
        # Every delivered frame disagrees with the last -- never settles.
        frames = [np.roll(base, shift=(i * 3, 0), axis=(0, 1)) for i in range(20)]
        waiter = _sequence_waiter(clock, frames)

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=1.0, stability_tolerance_px=1.0,
            stability_sample_count=3, sleep=clock.sleep, now=clock.now,
        )

        assert not result.ok
        assert result.status in (
            MotionAwareStatus.IMAGE_NOT_STABLE, MotionAwareStatus.SETTLE_TIMEOUT,
        )

    def test_settle_timeout_when_not_even_one_full_window_is_gathered(self) -> None:
        base = _textured_image((40, 40), seed=5)
        clock = _FakeClock(step_s=1.0)  # each delivery costs a whole second
        waiter = _sequence_waiter(clock, [base.copy() for _ in range(10)])

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=1.5, stability_tolerance_px=1.0,
            stability_sample_count=5, sleep=clock.sleep, now=clock.now,
        )

        assert result.status is MotionAwareStatus.SETTLE_TIMEOUT
        assert result.stability is None

    def test_underlying_capture_failure_is_propagated_as_capture_invalid(self) -> None:
        waiter = _failing_waiter(FrameAcquisitionStatus.CAMERA_UNAVAILABLE)

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=5.0, stability_tolerance_px=1.0,
        )

        assert result.status is MotionAwareStatus.CAPTURE_INVALID
        assert not result.ok
        assert result.diagnostics["capture_status"] == "camera_unavailable"

    def test_a_delivered_frame_whose_exposure_overlapped_motion_is_reported_distinctly(
        self,
    ) -> None:
        waiter = _failing_waiter(FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION)

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=5.0, stability_tolerance_px=1.0,
        )

        assert result.status is MotionAwareStatus.CAPTURE_INVALID
        assert result.diagnostics["capture_status"] == "exposure_overlapped_motion"

    def test_cancellation_stops_the_wait_and_calls_no_further_waiter_invocations(self) -> None:
        calls = 0

        def waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            nonlocal calls
            calls += 1
            return FrameAcquisitionResult(
                FrameAcquisitionStatus.OK,
                DeliveredFrame(pixels=np.zeros((5, 5)), captured_at_monotonic=0.0,
                                exposure_seconds=0.0),
            )

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=10.0, stability_tolerance_px=1.0,
            cancelled=lambda: True,
        )

        assert result.status is MotionAwareStatus.CANCELLED
        assert calls == 0

    def test_movement_context_is_threaded_into_diagnostics(self) -> None:
        waiter = _failing_waiter(FrameAcquisitionStatus.TIMEOUT)
        context = CommandedMovementContext(
            movement_type="pulse", duration_ms=500, axis="AXIS1", direction="POSITIVE",
        )

        result = acquire_verified_frame(
            waiter, reference_monotonic=0.0, timeout_s=1.0, stability_tolerance_px=1.0,
            movement_context=context,
        )

        assert result.diagnostics["movement_type"] == "pulse"
        assert result.diagnostics["duration_ms"] == 500
        assert result.diagnostics["axis"] == "AXIS1"
        assert result.diagnostics["direction"] == "POSITIVE"


class TestAcquireVerifiedFrames:
    def test_independent_per_source_outcomes(self) -> None:
        base = _textured_image((50, 50), seed=6)
        clock = _FakeClock()
        stable_waiter = _sequence_waiter(clock, [base.copy() for _ in range(6)])
        failing_waiter = _failing_waiter(FrameAcquisitionStatus.CAMERA_UNAVAILABLE)

        results = acquire_verified_frames(
            {"left": stable_waiter, "right": failing_waiter},
            reference_monotonic=0.0, timeout_s=5.0, stability_tolerance_px=1.0,
            stability_sample_count=3, sleep=clock.sleep, now=clock.now,
        )

        assert results["left"].ok
        assert not results["right"].ok
        assert results["right"].status is MotionAwareStatus.CAPTURE_INVALID

    def test_camera_count_independence_with_many_sources(self) -> None:
        base = _textured_image((40, 40), seed=7)
        clock = _FakeClock()

        def make_waiter() -> StableFrameWaiter:
            return _sequence_waiter(clock, [base.copy() for _ in range(6)])

        sources = {name: make_waiter() for name in ("main", "guide", "finder", "oag")}

        results = acquire_verified_frames(
            sources, reference_monotonic=0.0, timeout_s=5.0, stability_tolerance_px=1.0,
            stability_sample_count=3, sleep=clock.sleep, now=clock.now,
        )

        assert set(results) == {"main", "guide", "finder", "oag"}
        assert all(result.ok for result in results.values())
