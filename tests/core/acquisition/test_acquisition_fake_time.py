"""Issue #53: stable-frame and motion-aware acquisition driven through the
injected `Clock` (a FakeClock) -- deadline boundaries (just before / exactly
at / just after) and cancellation in each wait state, with no real waits."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.acquisition.motion_aware_acquisition import (
    MotionAwareStatus,
    acquire_verified_frame,
)
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
    StableFrameWaiter,
    acquire_settled_frames,
    acquire_stable_frame,
)
from astrotool_core.timing import FakeClock

_PIXELS = np.zeros((2, 2), dtype=np.float32)


class _ScheduledSource:
    """A frame source on fake time: each frame arrives at its scheduled
    moment. `next_frame(timeout)` advances the clock to the next arrival when
    it falls inside the wait window, otherwise waits out the window and
    returns None -- the contract of a mailbox `wait` with a timeout."""

    def __init__(self, clock: FakeClock, arrivals: list[tuple[float, float]]) -> None:
        #: (arrival time, exposure start) per frame
        self._clock = clock
        self._pending = list(arrivals)
        self.calls = 0

    def next_frame(self, timeout_s: float) -> DeliveredFrame | None:
        self.calls += 1
        now = self._clock.monotonic()
        if self._pending and self._pending[0][0] <= now + timeout_s:
            arrival, exposure_start = self._pending.pop(0)
            self._clock.advance(max(0.0, arrival - now))
            return DeliveredFrame(
                pixels=_PIXELS,
                captured_at_monotonic=arrival,
                exposure_seconds=arrival - exposure_start,
            )
        self._clock.advance(timeout_s)
        return None


def _stable(
    clock: FakeClock,
    source: _ScheduledSource,
    *,
    reference: float = 0.0,
    timeout_s: float = 3.0,
    cancelled: list[bool] | None = None,
) -> FrameAcquisitionResult:
    return acquire_stable_frame(
        source.next_frame,
        is_available=lambda: True,
        reference_monotonic=reference,
        timeout_s=timeout_s,
        cancelled=(lambda: bool(cancelled)) if cancelled is not None else None,
        clock=clock,
    )


class TestStableFrameDeadline:
    """The production deadline check decides here, not the test double: the
    source below ignores the timeout it is given and always delivers its
    next frame exactly 1 s after the call (frames 1-3 overlap the motion,
    frame 4 is valid). Calls start at t=0, 1, 2, 3; a 4th call happens only
    if production still sees time left at t=3 -- i.e. only if `t < deadline`.
    Mutating production's `remaining <= 0` to `< 0` makes the "exactly at"
    case request (and accept) the 4th frame."""

    @pytest.mark.parametrize(
        ("timeout_s", "calls", "expected"),
        [
            (3.001, 4, FrameAcquisitionStatus.OK),  # deadline just after t=3
            (3.0, 3, FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION),  # exactly at t=3
            (2.999, 3, FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION),  # just before
        ],
    )
    def test_a_frame_is_requested_only_while_time_is_left(
        self, timeout_s: float, calls: int, expected: FrameAcquisitionStatus
    ) -> None:
        clock = FakeClock()
        requested_at: list[float] = []

        def next_frame(_timeout_s: float) -> DeliveredFrame:
            requested_at.append(clock.monotonic())
            clock.advance(1.0)
            exposure_start = 0.0 if len(requested_at) < 4 else clock.monotonic()
            return DeliveredFrame(_PIXELS, clock.monotonic(), clock.monotonic() - exposure_start)

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: True,
            reference_monotonic=0.5,
            timeout_s=timeout_s,
            clock=clock,
        )

        assert result.status is expected
        assert requested_at == [float(i) for i in range(calls)]

    @pytest.mark.parametrize(
        ("arrival", "expected"),
        [
            (2.999, FrameAcquisitionStatus.OK),
            (3.001, FrameAcquisitionStatus.TIMEOUT),
        ],
    )
    def test_a_mailbox_style_source_times_out_with_the_window(
        self, arrival: float, expected: FrameAcquisitionStatus
    ) -> None:
        clock = FakeClock()
        source = _ScheduledSource(clock, [(arrival, arrival)])
        assert _stable(clock, source).status is expected
        assert clock.monotonic() == pytest.approx(min(arrival, 3.0))

    def test_only_overlapping_frames_before_the_deadline_is_reported_as_such(self) -> None:
        clock = FakeClock()
        # Two frames inside the window whose exposures began before t=0.5,
        # then a valid one just after the deadline.
        source = _ScheduledSource(clock, [(1.0, 0.4), (2.0, 0.4), (3.001, 3.0)])
        result = _stable(clock, source, reference=0.5)
        assert result.status is FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION


class TestStableFrameExposureBoundary:
    """A frame is valid only if its exposure started at or after the reference."""

    @pytest.mark.parametrize(
        ("exposure_start", "ok"),
        [(0.999, False), (1.0, True), (1.001, True)],
    )
    def test_exposure_start_around_the_reference(self, exposure_start: float, ok: bool) -> None:
        clock = FakeClock()
        source = _ScheduledSource(clock, [(1.5, exposure_start)])
        result = _stable(clock, source, reference=1.0, timeout_s=2.0)
        assert result.ok is ok


class TestStableFrameCancellation:
    def test_cancel_between_frames_stops_before_the_next_wait(self) -> None:
        clock = FakeClock()
        cancelled: list[bool] = []
        source = _ScheduledSource(clock, [(1.0, 0.0), (2.0, 2.0)])
        clock.call_at(1.0, lambda: cancelled.append(True))  # Stop at the first frame

        result = _stable(clock, source, reference=0.5, cancelled=cancelled)

        assert result.status is FrameAcquisitionStatus.CANCELLED
        assert source.calls == 1  # never waited for the valid second frame
        assert clock.monotonic() == 1.0


class TestSettledFramesOnFakeTime:
    def test_settle_waits_on_the_clock_and_stage_two_uses_the_settled_time(self) -> None:
        clock = FakeClock(10.0)
        references: list[float] = []

        def waiter(reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            references.append(reference)
            frame = DeliveredFrame(_PIXELS, clock.monotonic(), 0.0)
            return FrameAcquisitionResult(FrameAcquisitionStatus.OK, frame)

        results = acquire_settled_frames(
            {"left": waiter}, reference_monotonic=10.0, timeout_s=2.0, settle_ms=750, clock=clock
        )

        assert results["left"].ok
        assert clock.sleeps == [0.75]
        assert references == [10.0, 10.75]


def _textured(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).normal(500.0, 80.0, size=(48, 48))


def _sample_waiter(
    clock: FakeClock, images: list[np.ndarray], draws: list[float]
) -> StableFrameWaiter:
    """Delivers `images` in turn (the last one repeats), instantly, recording
    the fake time of every draw."""

    def waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
        draws.append(clock.monotonic())
        image = images[min(len(draws) - 1, len(images) - 1)]
        frame = DeliveredFrame(image, clock.monotonic(), 0.0)
        return FrameAcquisitionResult(FrameAcquisitionStatus.OK, frame)

    return waiter


class TestVerifiedFrameDeadline:
    """Sample interval 0.25 s, frames that never settle (a new random texture
    each draw). Draws happen at t=0, .25, .5, ...; a draw is attempted only
    while time is strictly before the deadline."""

    @pytest.mark.parametrize(("timeout_s", "draws"), [(0.749, 3), (0.75, 3), (0.751, 4)])
    def test_number_of_draws_around_the_deadline(self, timeout_s: float, draws: int) -> None:
        clock = FakeClock()
        drawn_at: list[float] = []
        images = [_textured(seed) for seed in range(10)]

        result = acquire_verified_frame(
            _sample_waiter(clock, images, drawn_at),
            reference_monotonic=0.0,
            timeout_s=timeout_s,
            stability_tolerance_px=0.5,
            stability_sample_count=3,
            stability_sample_interval_s=0.25,
            clock=clock,
        )

        assert result.status is MotionAwareStatus.IMAGE_NOT_STABLE
        assert len(drawn_at) == draws
        assert drawn_at == [0.25 * i for i in range(draws)]

    def test_a_still_image_is_accepted_after_exactly_one_window(self) -> None:
        clock = FakeClock()
        drawn_at: list[float] = []

        result = acquire_verified_frame(
            _sample_waiter(clock, [_textured(1)], drawn_at),
            reference_monotonic=0.0,
            timeout_s=5.0,
            stability_tolerance_px=0.5,
            stability_sample_count=3,
            stability_sample_interval_s=0.25,
            clock=clock,
        )

        assert result.status is MotionAwareStatus.OK
        assert drawn_at == [0.0, 0.25, 0.5]
        assert clock.sleeps == [0.25, 0.25]


class TestVerifiedFrameCancellation:
    def test_cancel_during_the_sample_interval_wait_ends_before_the_next_draw(self) -> None:
        clock = FakeClock()
        drawn_at: list[float] = []
        cancelled: list[bool] = []
        clock.call_at(0.1, lambda: cancelled.append(True))  # inside the first 0.25 s wait
        images = [_textured(seed) for seed in range(10)]

        result = acquire_verified_frame(
            _sample_waiter(clock, images, drawn_at),
            reference_monotonic=0.0,
            timeout_s=5.0,
            stability_tolerance_px=0.5,
            stability_sample_count=3,
            stability_sample_interval_s=0.25,
            cancelled=lambda: bool(cancelled),
            clock=clock,
        )

        assert result.status is MotionAwareStatus.CANCELLED
        assert drawn_at == [0.0]
        # The interval wait is not interruptible today: it completes, and the
        # cancel is seen before the next draw.
        assert clock.monotonic() == 0.25

    def test_cancel_while_waiting_for_a_frame_reports_cancelled(self) -> None:
        clock = FakeClock()
        cancelled: list[bool] = []
        calls: list[float] = []

        def waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            calls.append(clock.monotonic())
            clock.advance(0.5)
            cancelled.append(True)  # Stop pressed while this camera wait ran
            return FrameAcquisitionResult(FrameAcquisitionStatus.CANCELLED)

        result = acquire_verified_frame(
            waiter,
            reference_monotonic=0.0,
            timeout_s=5.0,
            stability_tolerance_px=0.5,
            cancelled=lambda: bool(cancelled),
            clock=clock,
        )

        assert result.status is MotionAwareStatus.CAPTURE_INVALID
        assert result.diagnostics["capture_status"] == "cancelled"
        assert calls == [0.0]
