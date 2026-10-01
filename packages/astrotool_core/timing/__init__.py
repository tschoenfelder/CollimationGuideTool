"""Deterministic-time boundary (issue #53): an injectable monotonic clock
with a cancellable wait, deadlines, bounded polling, and a fake clock for
tests and simulators.

Policy code takes `clock: Clock | None = None` and falls back to
`SYSTEM_CLOCK`; tests pass a `FakeClock`. No Qt and no app imports here
(enforced by an import-linter contract).
"""

from astrotool_core.timing.clock import SYSTEM_CLOCK, Clock, SystemClock
from astrotool_core.timing.deadline import Deadline, PollOutcome, poll_until
from astrotool_core.timing.fake_clock import FakeClock

__all__ = [
    "SYSTEM_CLOCK",
    "Clock",
    "Deadline",
    "FakeClock",
    "PollOutcome",
    "SystemClock",
    "poll_until",
]
