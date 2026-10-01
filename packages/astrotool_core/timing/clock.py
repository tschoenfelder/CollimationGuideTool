"""The injectable time boundary (issue #53): `Clock` and its production
implementation `SystemClock`.

Application and policy code reads time and waits only through a `Clock`
it was given (an optional keyword argument defaulting to `SYSTEM_CLOCK`),
never through `time.monotonic()`/`time.sleep()` directly, so tests can
substitute `FakeClock` and drive every settle/retry/deadline instantly.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Monotonic time plus a cancellable wait.

    `sleep` returns True when the full duration elapsed and False when it
    ended early because `cancel` was (or already is) set. A zero duration
    does not wait (it returns False when `cancel` is already set); a
    negative duration raises `ValueError`, as `time.sleep` does.
    """

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float, cancel: threading.Event | None = None) -> bool: ...


class SystemClock:
    """The real clock: `time.monotonic()` and a wait that wakes up at once
    when `cancel` is set (`threading.Event.wait`) instead of sleeping on."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float, cancel: threading.Event | None = None) -> bool:
        # Same contract as the time.sleep calls this replaces: a negative
        # duration is an error, and sleep(0) still yields the thread.
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        if cancel is None:
            time.sleep(seconds)
            return True
        return not cancel.wait(seconds)


#: The shared production default. Stateless, so one instance serves everyone.
SYSTEM_CLOCK: Clock = SystemClock()
