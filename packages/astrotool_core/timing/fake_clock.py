"""FakeClock — a `Clock` whose time only moves when told to (issue #53).

Two modes, chosen per instance:

- **auto-advance** (default): `sleep(s)` advances fake time by `s` at once
  and returns. A worker thread therefore never blocks on fake time; a retry
  or settle loop runs to its end instantly, on whichever thread calls it.
  This is the mode for nearly every test: simple, and impossible to hang.
  To make something happen *during* a wait (a device finishing, a user
  pressing Stop), schedule it with `call_at`/`call_later`: the callback runs
  when fake time reaches its moment, inside the `sleep`/`advance` that
  crosses it. If the callback sets the sleeper's `cancel` event, that
  `sleep` stops at the callback's moment and returns False.

- **manual** (`auto_advance=False`): `sleep(s)` blocks the calling thread
  until the test calls `advance(...)` far enough (or the sleeper's `cancel`
  event is set). Use it when a test must observe a worker *while* it waits:
  `wait_for_sleepers(n)` returns once `n` threads are blocked in `sleep`,
  then the test inspects state and calls `advance`. Setting `cancel` from
  the test wakes the sleeper within a few milliseconds; `advance(0)` wakes
  it immediately.

Every `sleep` request is recorded in `sleeps` (requested seconds, in call
order) so tests can assert exactly which waits a policy performed. Like
`time.sleep`, a negative duration raises `ValueError`.

Limits (read before driving this from several threads, e.g. #51 simulators):

- Auto-advance models one timeline on which sleeps happen one after the
  other, never in parallel: two threads that each sleep 1 s concurrently
  move time by 2 s in total, and which of them "wakes" at t+1 depends on
  thread scheduling. Use manual mode when concurrent waits must overlap.
- Time moves under one advancing thread at a time (`sleep` in auto mode and
  `advance` are serialized), so callbacks always fire in time order. While a
  thread is advancing, another thread's auto-mode `sleep`/`advance` waits
  for it to finish.
- Callbacks run on the advancing thread, outside the clock's state lock: a
  callback may read the clock or schedule more callbacks, and may itself
  call auto-mode `sleep` (re-entrant). It must not block on another thread
  that is itself trying to sleep/advance on this clock (deadlock), and in
  manual mode it must not call `sleep` (nobody else can advance: deadlock).
"""

from __future__ import annotations

import heapq
import itertools
import threading
from collections.abc import Callable

#: Manual mode only: how often a blocked sleeper with a `cancel` event
#: re-checks it when nobody calls `advance`. A safety net; tests normally
#: call `advance(0)` after cancelling, which wakes sleepers at once.
_CANCEL_RECHECK_S = 0.005


class FakeClock:
    def __init__(self, start: float = 0.0, *, auto_advance: bool = True) -> None:
        self._now = float(start)
        self._auto_advance = auto_advance
        self._cond = threading.Condition(threading.RLock())
        #: Serializes whoever moves time (re-entrant for callbacks that sleep).
        self._advance_lock = threading.RLock()
        self._timers: list[tuple[float, int, Callable[[], object]]] = []
        self._timer_seq = itertools.count()
        self._blocked_sleepers = 0
        #: Requested duration of every `sleep` call, in call order.
        self.sleeps: list[float] = []

    # -- Clock protocol ----------------------------------------------------

    def monotonic(self) -> float:
        with self._cond:
            return self._now

    def sleep(self, seconds: float, cancel: threading.Event | None = None) -> bool:
        if seconds < 0:
            raise ValueError("sleep length must be non-negative")
        with self._cond:
            self.sleeps.append(seconds)
        if cancel is not None and cancel.is_set():
            return False
        if self._auto_advance:
            with self._advance_lock:
                with self._cond:
                    target = self._now + seconds
                self._advance_to(target, cancel)
            return not (cancel is not None and cancel.is_set())
        with self._cond:
            target = self._now + seconds
        return self._block_until(target, cancel)

    # -- test controls -----------------------------------------------------

    def advance(self, seconds: float) -> None:
        """Moves time forward by `seconds` (>= 0), firing due callbacks in
        time order and releasing manual-mode sleepers whose time has come."""
        if seconds < 0:
            raise ValueError("FakeClock cannot go backwards")
        with self._advance_lock:
            with self._cond:
                target = self._now + seconds
            self._advance_to(target, None)

    def call_at(self, when: float, callback: Callable[[], object]) -> None:
        """Runs `callback` once fake time reaches `when` (at once on the next
        `advance`/`sleep` if `when` is already past)."""
        with self._cond:
            heapq.heappush(self._timers, (when, next(self._timer_seq), callback))

    def call_later(self, delay_s: float, callback: Callable[[], object]) -> None:
        with self._cond:
            self.call_at(self._now + delay_s, callback)

    @property
    def blocked_sleepers(self) -> int:
        """Manual mode: number of threads currently blocked in `sleep`."""
        with self._cond:
            return self._blocked_sleepers

    def wait_for_sleepers(self, count: int = 1, *, timeout_s: float = 5.0) -> bool:
        """Manual mode: blocks (real time, at most `timeout_s`) until at least
        `count` threads are blocked in `sleep`. A synchronization barrier for
        the test thread, not a policy wait; returns as soon as it holds."""
        with self._cond:
            return self._cond.wait_for(lambda: self._blocked_sleepers >= count, timeout_s)

    # -- internals ---------------------------------------------------------

    def _advance_to(self, target: float, cancel: threading.Event | None) -> None:
        """Caller holds `_advance_lock`."""
        while True:
            with self._cond:
                if not self._timers or self._timers[0][0] > target:
                    self._now = max(self._now, target)
                    self._cond.notify_all()
                    return
                when, _seq, callback = heapq.heappop(self._timers)
                self._now = max(self._now, when)
                self._cond.notify_all()
            callback()  # outside the state lock, see "Limits" above
            if cancel is not None and cancel.is_set():
                return

    def _block_until(self, target: float, cancel: threading.Event | None) -> bool:
        with self._cond:
            self._blocked_sleepers += 1
            self._cond.notify_all()
            try:
                while self._now < target:
                    if cancel is not None and cancel.is_set():
                        return False
                    self._cond.wait(_CANCEL_RECHECK_S if cancel is not None else None)
            finally:
                self._blocked_sleepers -= 1
                self._cond.notify_all()
        return not (cancel is not None and cancel.is_set())
