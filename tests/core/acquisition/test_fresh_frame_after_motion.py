"""S6.7 (#52; #43/#30/#39): the one owner of "a measurement after motion uses a frame whose
exposure STARTED after the motion ended (+ settle), under an exposure-scaled deadline, with an
explicit timeout outcome" -- `astrotool_core.acquisition.fresh_frame_after_motion`.

Every frame has an explicit exposure start/length/delivery on a FakeClock (`FrameTimeline`,
#51), so stale backlogs, motion-overlapping exposures and long exposures are exact. The
production `acquire_stable_frame` decides each frame; the deadline is the service's."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.acquisition import (
    DEFAULT_FRESH_FRAME_BUDGET,
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
    FreshFrameBudget,
    FreshFrameSource,
    acquire_fresh_frame_after_motion,
    measurement_reference,
)
from astrotool_core.acquisition.fresh_frame_after_motion import MAX_MEASUREMENT_EXPOSURE_S
from astrotool_core.testing.sim_frames import FrameSpec, FrameTimeline
from astrotool_core.timing import FakeClock


def _timeline(start: float = 0.0) -> tuple[FakeClock, FrameTimeline]:
    clock = FakeClock(start)
    return clock, FrameTimeline(clock)


def _starts(timeline: FrameTimeline) -> list[float]:
    return [f.spec.exposure_start for f in timeline.delivered]


class TestTheBudget:
    @pytest.mark.parametrize(
        ("exposure_s", "expected"),
        [(None, 2.0), (0.0, 2.0), (0.4, 2.0), (0.5, 2.0), (0.6, 2.2), (2.0, 5.0), (5.0, 11.0)],
    )
    def test_two_exposures_plus_a_margin_never_below_the_floor(
        self, exposure_s: float | None, expected: float
    ) -> None:
        assert DEFAULT_FRESH_FRAME_BUDGET.timeout_s(exposure_s) == pytest.approx(expected)

    def test_an_assumed_exposure_applies_only_while_none_is_known(self) -> None:
        budget = FreshFrameBudget(assumed_exposure_s=2.5)
        assert budget.timeout_s(None) == pytest.approx(6.0)
        assert budget.timeout_s(0.1) == pytest.approx(2.0)

    def test_the_reference_is_the_motion_end_plus_the_settle(self) -> None:
        assert measurement_reference(10.0, 0.75) == pytest.approx(10.75)
        assert measurement_reference(10.0, -1.0) == pytest.approx(10.0)


class TestStaleAndOverlappingFramesAreNeverReturned:
    def test_a_stale_backlog_is_skipped(self) -> None:
        clock, timeline = _timeline(20.0)
        # Delivered long before the motion ended at t=20 (a queued backlog), then a fresh one.
        timeline.add(FrameSpec(5.0, 0.5), FrameSpec(10.0, 0.5), FrameSpec(20.2, 0.5))

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=20.0, clock=clock
        )

        assert result.ok, result
        assert result.frame is not None
        assert result.frame.captured_at_monotonic == pytest.approx(20.7)
        assert _starts(timeline) == [5.0, 10.0, 20.2]

    def test_an_exposure_overlapping_the_motion_or_the_settle_is_skipped(self) -> None:
        clock, timeline = _timeline(10.0)
        timeline.add_motion(8.0, 10.0)
        # Started during the motion / during the settle (delivered after both), then fresh.
        timeline.add(FrameSpec(9.5, 1.0), FrameSpec(10.5, 1.0), FrameSpec(11.0, 1.0))

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=10.0, settle_s=0.75, exposure_s=1.0, clock=clock
        )

        assert result.ok, result
        assert result.reference_monotonic == pytest.approx(10.75)
        assert result.frame is not None
        assert result.frame.captured_at_monotonic - result.frame.exposure_seconds >= 10.75
        assert [f.overlaps_motion for f in timeline.delivered] == [True, False, False]

    def test_a_frame_starting_exactly_at_the_reference_is_fresh(self) -> None:
        clock, timeline = _timeline(10.0)
        timeline.add(FrameSpec(10.75 - 1e-6, 0.5), FrameSpec(10.75, 0.5))

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=10.0, settle_s=0.75, clock=clock
        )

        assert result.ok
        assert _starts(timeline) == [pytest.approx(10.75 - 1e-6), 10.75]


class TestLongExposures:
    """T05 / #43 class: a fixed budget (autofocus 5 s, reacquisition 6 s) timed out for any
    exposure longer than ~2-3 s on a streaming camera -- the in-flight exposure must end and a
    whole fresh one follow."""

    @pytest.mark.parametrize("exposure_s", [2.5, 4.0, 8.0])
    def test_an_unknown_long_exposure_extends_the_budget_and_succeeds(
        self, exposure_s: float
    ) -> None:
        clock, timeline = _timeline(0.0)
        # Back-to-back stream; the motion ended just after a frame started (worst case).
        timeline.schedule_stream(first_start=0.0, exposure_s=exposure_s, cadence_s=exposure_s,
                                 count=10)
        clock.advance(exposure_s + 0.01)

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=clock.monotonic(), clock=clock
        )

        assert result.ok, result
        assert result.exposure_s == pytest.approx(exposure_s)
        assert result.timeout_s == pytest.approx(2 * exposure_s + 1.0)
        assert result.frame is not None
        assert result.frame.captured_at_monotonic == pytest.approx(3 * exposure_s)

    def test_a_known_long_exposure_is_budgeted_from_the_start(self) -> None:
        clock, timeline = _timeline(0.0)
        timeline.schedule_stream(first_start=0.0, exposure_s=6.0, cadence_s=6.0, count=5)
        clock.advance(0.01)

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=0.01, exposure_s=6.0, clock=clock
        )

        assert result.ok
        assert result.timeout_s == pytest.approx(13.0)
        assert result.waited_s == pytest.approx(12.0 - 0.01)

    def test_the_source_remembers_the_exposure_for_the_next_wait(self) -> None:
        clock, timeline = _timeline(0.0)
        timeline.schedule_stream(first_start=0.0, exposure_s=3.0, cadence_s=3.0, count=20)
        clock.advance(2.0)  # the in-flight frame arrives inside the 2 s floor and teaches 3 s
        source = FreshFrameSource(timeline.waiter(), clock=clock)

        assert source.after_motion().ok
        second = source.after_motion()

        assert second.ok
        assert second.exposure_s == pytest.approx(3.0)
        assert second.timeout_s == pytest.approx(7.0)

    def test_an_unknown_exposure_longer_than_the_budget_with_nothing_delivered_times_out(
        self,
    ) -> None:
        # Nothing can teach the exposure inside the 2 s floor: an explicit TIMEOUT, never a
        # stale frame -- callers that may see such exposures pass a hint or an assumed one.
        clock, timeline = _timeline(0.0)
        timeline.schedule_stream(first_start=0.0, exposure_s=3.0, cadence_s=3.0, count=5)

        result = FreshFrameSource(timeline.waiter(), clock=clock).after_motion()
        assumed = FreshFrameSource(
            timeline.waiter(), clock=clock, budget=FreshFrameBudget(assumed_exposure_s=3.0)
        ).after_motion()

        assert result.status is FrameAcquisitionStatus.TIMEOUT and result.waited_s == 2.0
        assert assumed.ok, assumed

    def test_an_exposure_hint_wins_over_the_learned_exposure(self) -> None:
        clock, timeline = _timeline(0.0)
        timeline.schedule_stream(first_start=0.0, exposure_s=1.0, cadence_s=1.0, count=20)
        source = FreshFrameSource(timeline.waiter(), clock=clock, exposure_hint=lambda: 4.0)

        result = source.after_motion()

        assert result.ok and result.timeout_s == pytest.approx(9.0)


class TestExplicitOutcomes:
    def test_no_frame_at_all_is_an_explicit_timeout_after_the_floor(self) -> None:
        clock, timeline = _timeline(10.0)

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=10.0, clock=clock
        )

        assert result.status is FrameAcquisitionStatus.TIMEOUT
        assert result.timed_out and not result.ok and result.frame is None
        assert result.waited_s == pytest.approx(2.0)
        assert "no frame arrived" in result.describe()

    def test_only_overlapping_frames_is_its_own_timeout_outcome(self) -> None:
        clock, timeline = _timeline(10.0)
        timeline.add(FrameSpec(9.9, 0.5))  # overlaps the motion end; nothing fresh follows

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=10.0, exposure_s=0.5, clock=clock
        )

        assert result.status is FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION
        assert result.timed_out
        assert result.waited_s == pytest.approx(2.0)

    @pytest.mark.parametrize(("start", "ok"), [(11.99, True), (12.0, True), (12.01, False)])
    def test_the_deadline_boundary_is_decided_by_the_service(self, start: float, ok: bool) -> None:
        # exposure 0.5 s -> budget 2.0 s from the reference 10.0 -> deadline 12.0... the frame
        # delivered at start + 0.5 must arrive by then; FrameTimeline delivers at `captured_at`.
        clock, timeline = _timeline(10.0)
        timeline.add(FrameSpec(start - 0.5, 0.5))

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=10.0, exposure_s=0.5, clock=clock
        )

        assert result.ok is ok, result
        if not ok:
            assert result.status is FrameAcquisitionStatus.TIMEOUT
            assert clock.monotonic() == pytest.approx(12.0)

    def test_an_unavailable_camera_is_reported_at_once(self) -> None:
        clock, timeline = _timeline(10.0)

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(available=False), motion_ended_at=10.0, clock=clock
        )

        assert result.status is FrameAcquisitionStatus.CAMERA_UNAVAILABLE
        assert not result.timed_out and result.waited_s == 0.0

    def test_cancel_ends_the_wait(self) -> None:
        clock, timeline = _timeline(10.0)
        calls: list[float] = []

        def waiter(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            calls.append(timeout_s)
            clock.sleep(timeout_s)
            return FrameAcquisitionResult(
                FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION, longest_exposure_s=4.0
            )

        result = acquire_fresh_frame_after_motion(
            waiter, motion_ended_at=10.0, clock=clock, cancelled=lambda: bool(calls)
        )

        assert result.status is FrameAcquisitionStatus.CANCELLED
        assert len(calls) == 1

    def test_a_future_settle_does_not_eat_into_the_budget(self) -> None:
        clock, timeline = _timeline(10.0)
        # Settle of 3 s, exposure 0.5 s: the deadline is reference (13) + 2 s, not now + 2 s.
        timeline.add(FrameSpec(14.4, 0.5))

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=10.0, settle_s=3.0, exposure_s=0.5, clock=clock
        )

        assert result.ok, result


def test_the_stable_frame_result_reports_the_longest_exposure_seen() -> None:
    clock, timeline = _timeline(10.0)
    timeline.add(FrameSpec(8.0, 2.5), FrameSpec(9.6, 0.5))

    result = timeline.waiter()(10.0, 1.0)

    assert result.status is FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION
    assert result.longest_exposure_s == pytest.approx(2.5)
    ok = FrameAcquisitionResult(
        FrameAcquisitionStatus.OK, DeliveredFrame(np.zeros((2, 2)), 1.0, 0.1)
    )
    assert ok.longest_exposure_s is None  # a result built without a wait knows nothing


class TestStopAndTheCap:
    """S6.7 review: a long wait must honour Stop within one slice, and no wait may exceed the
    absolute cap however long the exposure."""

    def test_stop_during_a_long_wait_returns_cancelled_within_one_slice(self) -> None:
        clock, timeline = _timeline(0.0)
        timeline.schedule_stream(first_start=0.0, exposure_s=20.0, cadence_s=20.0, count=3)
        stop_at = 7.3
        clock.advance(0.01)

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=0.01, exposure_s=20.0, clock=clock,
            cancelled=lambda: clock.monotonic() >= stop_at,
        )

        assert result.status is FrameAcquisitionStatus.CANCELLED
        assert stop_at <= clock.monotonic() <= stop_at + DEFAULT_FRESH_FRAME_BUDGET.slice_s

    def test_the_waiter_is_never_asked_for_more_than_one_slice(self) -> None:
        clock, timeline = _timeline(10.0)
        asked: list[float] = []
        waiter = timeline.waiter()

        def recording(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            asked.append(timeout_s)
            return waiter(reference, timeout_s)

        result = acquire_fresh_frame_after_motion(
            recording, motion_ended_at=10.0, exposure_s=3.0, clock=clock
        )

        assert result.status is FrameAcquisitionStatus.TIMEOUT
        assert result.waited_s == pytest.approx(7.0)
        assert max(asked) <= DEFAULT_FRESH_FRAME_BUDGET.slice_s + 1e-9
        assert len(asked) == 14

    @pytest.mark.parametrize("exposure_s", [MAX_MEASUREMENT_EXPOSURE_S, 45.0, 60.0, 600.0])
    def test_no_wait_exceeds_the_cap(self, exposure_s: float) -> None:
        cap = 2.0 * MAX_MEASUREMENT_EXPOSURE_S + 1.0
        assert DEFAULT_FRESH_FRAME_BUDGET.timeout_s(exposure_s) == pytest.approx(cap)
        clock, timeline = _timeline(0.0)
        timeline.add(FrameSpec(-1.0, exposure_s))  # in flight: reveals the long exposure

        result = acquire_fresh_frame_after_motion(
            timeline.waiter(), motion_ended_at=0.0, clock=clock
        )

        assert result.timed_out
        assert result.waited_s <= cap + 1e-9
