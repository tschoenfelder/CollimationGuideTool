"""Direct unit coverage for `astrotool_core.acquisition.stable_frame_acquisition`
-- issue #27's "A" layer, extracted out of `MountTestMovePanel`/`CameraPanel`.
Deliberately uses only plain callables and a fake clock -- no cameras, no
FITS data, no Qt, no mount/INDI code -- see the module's own docstring."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
    StableFrameWaiter,
    acquire_settled_frames,
    acquire_stable_frame,
)

_PIXELS = np.zeros((2, 2), dtype=np.float32)


class _FakeClock:
    """A monotonic clock a test fully controls -- every call to `next_frame`
    advances it by `step_s`, so a deadline can be crossed deterministically
    without any real sleeping."""

    def __init__(self, *, step_s: float = 0.1) -> None:
        self._now = 0.0
        self._step_s = step_s

    def now(self) -> float:
        return self._now

    def tick(self) -> float:
        self._now += self._step_s
        return self._now


def _frame(captured_at: float, *, exposure_seconds: float = 0.0) -> DeliveredFrame:
    return DeliveredFrame(
        pixels=_PIXELS, captured_at_monotonic=captured_at, exposure_seconds=exposure_seconds
    )


class TestAcquireStableFrame:
    def test_accepts_a_frame_whose_exposure_started_after_the_reference(self) -> None:
        clock = _FakeClock()

        def next_frame(_timeout_s: float) -> DeliveredFrame | None:
            return _frame(clock.tick(), exposure_seconds=0.0)

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: True,
            reference_monotonic=0.0,
            timeout_s=5.0,
            now=clock.now,
        )

        assert result.ok
        assert result.status is FrameAcquisitionStatus.OK
        assert result.frame is not None
        assert result.frame.pixels is _PIXELS

    def test_skips_a_frame_whose_exposure_overlapped_the_reference_and_accepts_the_next_one(
        self,
    ) -> None:
        """Real report, diagnostic c7dc2c3d ("still using frames during
        movement"): a frame delivered after the reference can still have
        *started* its own exposure before it -- must be skipped, not
        accepted, even though a later frame arrives fine."""
        clock = _FakeClock()
        deliveries = iter(
            [
                _frame(0.05, exposure_seconds=0.2),  # exposure started at -0.15 -- overlaps
                _frame(clock.tick() + 1.0, exposure_seconds=0.0),  # starts well after reference
            ]
        )

        def next_frame(_timeout_s: float) -> DeliveredFrame | None:
            clock.tick()
            return next(deliveries)

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: True,
            reference_monotonic=0.0,
            timeout_s=5.0,
            now=clock.now,
        )

        assert result.ok

    def test_reports_camera_unavailable_without_ever_calling_next_frame(self) -> None:
        calls = 0

        def next_frame(_timeout_s: float) -> DeliveredFrame | None:
            nonlocal calls
            calls += 1
            return _frame(0.0)

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: False,
            reference_monotonic=0.0,
            timeout_s=5.0,
        )

        assert result.status is FrameAcquisitionStatus.CAMERA_UNAVAILABLE
        assert not result.ok
        assert calls == 0

    def test_times_out_when_no_frame_ever_arrives(self) -> None:
        clock = _FakeClock(step_s=1.0)

        def next_frame(_timeout_s: float) -> DeliveredFrame | None:
            clock.tick()
            return None

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: True,
            reference_monotonic=0.0,
            timeout_s=2.0,
            now=clock.now,
        )

        assert result.status is FrameAcquisitionStatus.TIMEOUT

    def test_reports_exposure_overlapped_motion_when_only_overlapping_frames_ever_arrive(
        self,
    ) -> None:
        """A source that keeps delivering fresh frames that never actually
        satisfy the reference (e.g. its exposure is longer than the whole
        wait budget) times out too -- but with a status naming the real
        cause, not a generic timeout."""
        clock = _FakeClock(step_s=1.0)

        def next_frame(_timeout_s: float) -> DeliveredFrame | None:
            now = clock.tick()
            return _frame(now, exposure_seconds=100.0)  # always overlaps

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: True,
            reference_monotonic=0.0,
            timeout_s=2.0,
            now=clock.now,
        )

        assert result.status is FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION

    def test_reports_cancelled_and_never_calls_next_frame_once_cancelled(self) -> None:
        calls = 0

        def next_frame(_timeout_s: float) -> DeliveredFrame | None:
            nonlocal calls
            calls += 1
            return _frame(0.0)

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: True,
            reference_monotonic=0.0,
            timeout_s=5.0,
            cancelled=lambda: True,
        )

        assert result.status is FrameAcquisitionStatus.CANCELLED
        assert calls == 0


def _always(status: FrameAcquisitionStatus) -> FrameAcquisitionResult:
    frame = _frame(0.0) if status is FrameAcquisitionStatus.OK else None
    return FrameAcquisitionResult(status, frame)


class TestAcquireSettledFrames:
    def test_all_sources_succeed_after_settling(self) -> None:
        calls: dict[str, list[float]] = {"left": [], "right": []}

        def make_waiter(key: str) -> StableFrameWaiter:
            def waiter(reference: float, _timeout_s: float) -> FrameAcquisitionResult:
                calls[key].append(reference)
                return _always(FrameAcquisitionStatus.OK)

            return waiter

        results = acquire_settled_frames(
            {"left": make_waiter("left"), "right": make_waiter("right")},
            reference_monotonic=0.0,
            timeout_s=1.0,
            settle_ms=0,
            sleep=lambda _s: None,
        )

        assert all(result.ok for result in results.values())
        assert len(calls["left"]) == 2  # stage 1 + stage 2
        assert len(calls["right"]) == 2

    def test_settle_sleeps_between_stage_one_and_stage_two(self) -> None:
        clock = _FakeClock(step_s=0.0)
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock.tick()  # simulate time actually elapsing while asleep

        def waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            return _always(FrameAcquisitionStatus.OK)

        acquire_settled_frames(
            {"left": waiter},
            reference_monotonic=0.0,
            timeout_s=1.0,
            settle_ms=250,
            sleep=sleep,
            now=clock.now,
        )

        assert sleeps == [0.25]

    def test_stage_two_is_never_attempted_when_any_source_fails_stage_one(self) -> None:
        stage1_calls = 0
        stage2_calls_after_failure = 0

        def failing_waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            nonlocal stage1_calls
            stage1_calls += 1
            return _always(FrameAcquisitionStatus.TIMEOUT)

        def ok_waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            nonlocal stage2_calls_after_failure
            stage2_calls_after_failure += 1
            return _always(FrameAcquisitionStatus.OK)

        results = acquire_settled_frames(
            {"left": failing_waiter, "right": ok_waiter},
            reference_monotonic=0.0,
            timeout_s=1.0,
            settle_ms=250,
            sleep=lambda _s: pytest.fail("settle must not sleep when stage 1 already failed"),
        )

        assert not results["left"].ok
        assert results["left"].status is FrameAcquisitionStatus.TIMEOUT
        assert stage1_calls == 1
        # ok_waiter only ever called once too (its own stage-1 call) --
        # stage 2 never runs for *any* source once one has failed.
        assert stage2_calls_after_failure == 1

    def test_stage_one_failure_status_is_reported_unchanged(self) -> None:
        def unavailable_waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            return _always(FrameAcquisitionStatus.CAMERA_UNAVAILABLE)

        results = acquire_settled_frames(
            {"left": unavailable_waiter},
            reference_monotonic=0.0,
            timeout_s=1.0,
            settle_ms=0,
            sleep=lambda _s: None,
        )

        assert results["left"].status is FrameAcquisitionStatus.CAMERA_UNAVAILABLE

    def test_stage_two_failure_is_reported_as_settle_not_reached(self) -> None:
        calls = 0

        def flaky_waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            nonlocal calls
            calls += 1
            # Succeeds the first time (stage 1), times out the second (stage 2).
            status = FrameAcquisitionStatus.OK if calls == 1 else FrameAcquisitionStatus.TIMEOUT
            return _always(status)

        results = acquire_settled_frames(
            {"left": flaky_waiter},
            reference_monotonic=0.0,
            timeout_s=1.0,
            settle_ms=10,
            sleep=lambda _s: None,
        )

        assert results["left"].status is FrameAcquisitionStatus.SETTLE_NOT_REACHED

    def test_is_independent_of_how_many_sources_are_configured(self) -> None:
        def waiter(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            return _always(FrameAcquisitionStatus.OK)

        results = acquire_settled_frames(
            {"main": waiter, "guide": waiter, "finder": waiter, "oag": waiter},
            reference_monotonic=0.0,
            timeout_s=1.0,
            settle_ms=0,
            sleep=lambda _s: None,
        )

        assert set(results) == {"main", "guide", "finder", "oag"}
        assert all(result.ok for result in results.values())
