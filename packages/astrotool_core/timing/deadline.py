"""Deadlines and bounded polling on top of an injected `Clock` (issue #53).

Boundary convention, used everywhere in this project's timing code: a
deadline is *expired* once `now >= expires_at` — exactly at the deadline
counts as expired, just before it does not.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from astrotool_core.timing.clock import SYSTEM_CLOCK, Clock


@dataclass(frozen=True)
class Deadline:
    """A point in a clock's monotonic time after which an operation gives up."""

    clock: Clock
    expires_at: float

    @classmethod
    def after(cls, timeout_s: float, *, clock: Clock | None = None) -> Deadline:
        """The deadline `timeout_s` from now on `clock` (default: the real clock)."""
        source = clock or SYSTEM_CLOCK
        return cls(source, source.monotonic() + timeout_s)

    def remaining(self) -> float:
        """Seconds left, never negative (0.0 once expired)."""
        return max(0.0, self.expires_at - self.clock.monotonic())

    def expired(self) -> bool:
        return self.clock.monotonic() >= self.expires_at


class PollOutcome(Enum):
    #: The predicate became true (possibly on the very first check).
    SATISFIED = "satisfied"
    #: The deadline expired with the predicate still false.
    TIMED_OUT = "timed_out"
    #: `cancel` was set before or during a wait.
    CANCELLED = "cancelled"

    @property
    def ok(self) -> bool:
        return self is PollOutcome.SATISFIED


def poll_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float,
    interval_s: float,
    clock: Clock | None = None,
    cancel: threading.Event | None = None,
) -> PollOutcome:
    """Checks `predicate` until it is true, the deadline expires, or `cancel`
    is set. Order per round: check predicate -> check deadline -> wait
    `interval_s`. So the predicate is always checked once, even with a zero
    timeout, and once more after the last wait before reporting a timeout.

    The deadline is taken when polling starts. A wait is never shortened to
    the remaining time, so the last check may happen up to `interval_s`
    after the deadline (the behavior of the hand-written loops this replaces).
    """
    source = clock or SYSTEM_CLOCK
    deadline = Deadline.after(timeout_s, clock=source)
    while True:
        if cancel is not None and cancel.is_set():
            return PollOutcome.CANCELLED
        if predicate():
            return PollOutcome.SATISFIED
        if deadline.expired():
            return PollOutcome.TIMED_OUT
        if not source.sleep(interval_s, cancel):
            return PollOutcome.CANCELLED
