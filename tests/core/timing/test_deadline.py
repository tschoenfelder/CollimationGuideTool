"""Deadline and poll_until on fake time (issue #53): exact boundaries and
cancellation in every poll state."""

from __future__ import annotations

import threading

import pytest
from astrotool_core.timing import Deadline, FakeClock, PollOutcome, poll_until


class TestDeadlineBoundary:
    """Expired means `now >= expires_at`."""

    @pytest.mark.parametrize(
        ("elapsed", "expired", "remaining"),
        [
            (2.999, False, 0.001),  # just before
            (3.0, True, 0.0),  # exactly at
            (3.001, True, 0.0),  # just after (remaining never goes negative)
        ],
    )
    def test_boundary(self, elapsed: float, expired: bool, remaining: float) -> None:
        clock = FakeClock(50.0)
        deadline = Deadline.after(3.0, clock=clock)
        clock.advance(elapsed)
        assert deadline.expired() is expired
        assert deadline.remaining() == pytest.approx(remaining)

    def test_zero_timeout_is_expired_immediately(self) -> None:
        clock = FakeClock()
        assert Deadline.after(0.0, clock=clock).expired()

    def test_defaults_to_the_real_clock(self) -> None:
        deadline = Deadline.after(60.0)
        assert not deadline.expired()
        assert 0.0 < deadline.remaining() <= 60.0


class _CountingPredicate:
    def __init__(self, true_from_call: int | None) -> None:
        self.calls = 0
        self._true_from = true_from_call

    def __call__(self) -> bool:
        self.calls += 1
        return self._true_from is not None and self.calls >= self._true_from


class TestPollUntil:
    def test_satisfied_on_the_first_check_never_waits(self) -> None:
        clock = FakeClock()
        outcome = poll_until(lambda: True, timeout_s=5.0, interval_s=0.1, clock=clock)
        assert outcome is PollOutcome.SATISFIED
        assert outcome.ok
        assert clock.sleeps == []

    def test_zero_timeout_checks_exactly_once(self) -> None:
        clock = FakeClock()
        predicate = _CountingPredicate(true_from_call=None)
        outcome = poll_until(predicate, timeout_s=0.0, interval_s=0.05, clock=clock)
        assert outcome is PollOutcome.TIMED_OUT
        assert not outcome.ok
        assert predicate.calls == 1
        assert clock.sleeps == []

    def test_times_out_after_the_deadline_with_one_last_check(self) -> None:
        clock = FakeClock()
        predicate = _CountingPredicate(true_from_call=None)
        outcome = poll_until(predicate, timeout_s=0.2, interval_s=0.05, clock=clock)
        assert outcome is PollOutcome.TIMED_OUT
        # checks at t=0, .05, .10, .15, .20 -> expired at exactly .20
        assert predicate.calls == 5
        assert clock.sleeps == [0.05] * 4
        assert clock.monotonic() == pytest.approx(0.2)

    def test_satisfied_by_the_check_exactly_at_the_deadline(self) -> None:
        clock = FakeClock()
        predicate = _CountingPredicate(true_from_call=5)  # the t=.20 check
        outcome = poll_until(predicate, timeout_s=0.2, interval_s=0.05, clock=clock)
        assert outcome is PollOutcome.SATISFIED

    def test_condition_arriving_just_before_the_deadline_is_seen(self) -> None:
        clock = FakeClock()
        ready = threading.Event()
        clock.call_at(0.199, ready.set)
        outcome = poll_until(ready.is_set, timeout_s=0.2, interval_s=0.05, clock=clock)
        assert outcome is PollOutcome.SATISFIED

    def test_condition_arriving_just_after_the_deadline_is_too_late(self) -> None:
        clock = FakeClock()
        ready = threading.Event()
        clock.call_at(0.201, ready.set)
        outcome = poll_until(ready.is_set, timeout_s=0.2, interval_s=0.05, clock=clock)
        assert outcome is PollOutcome.TIMED_OUT
        assert not ready.is_set()  # time stopped at the deadline, before it

    def test_cancelled_before_the_first_check(self) -> None:
        clock = FakeClock()
        cancel = threading.Event()
        cancel.set()
        predicate = _CountingPredicate(true_from_call=1)
        outcome = poll_until(predicate, timeout_s=1.0, interval_s=0.1, clock=clock, cancel=cancel)
        assert outcome is PollOutcome.CANCELLED
        assert predicate.calls == 0

    def test_cancelled_during_a_wait_stops_at_that_moment(self) -> None:
        clock = FakeClock()
        cancel = threading.Event()
        clock.call_at(0.125, cancel.set)  # in the middle of the third wait
        predicate = _CountingPredicate(true_from_call=None)
        outcome = poll_until(predicate, timeout_s=1.0, interval_s=0.05, clock=clock, cancel=cancel)
        assert outcome is PollOutcome.CANCELLED
        assert clock.monotonic() == pytest.approx(0.125)
        assert predicate.calls == 3  # t=0, .05, .10; never checked again

    def test_cancelled_by_the_predicate_itself_between_waits(self) -> None:
        clock = FakeClock()
        cancel = threading.Event()

        def predicate() -> bool:
            cancel.set()  # e.g. a Stop pressed while a status read was in flight
            return False

        outcome = poll_until(predicate, timeout_s=1.0, interval_s=0.05, clock=clock, cancel=cancel)
        assert outcome is PollOutcome.CANCELLED
        assert clock.monotonic() == 0.0
