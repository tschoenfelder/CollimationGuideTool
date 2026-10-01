"""FakeClock and SystemClock (issue #53).

Real time here is limited to a few milliseconds of SystemClock checks and
the bounded thread barriers of manual mode (each returns as soon as the
awaited state holds)."""

from __future__ import annotations

import threading

import pytest
from astrotool_core.timing import SYSTEM_CLOCK, Clock, FakeClock, SystemClock


class TestProtocol:
    def test_both_clocks_satisfy_the_protocol(self) -> None:
        assert isinstance(SYSTEM_CLOCK, Clock)
        assert isinstance(FakeClock(), Clock)


class TestAutoAdvance:
    def test_sleep_advances_instantly_and_is_recorded(self) -> None:
        clock = FakeClock(10.0)
        assert clock.sleep(1.5) is True
        assert clock.sleep(0.25) is True
        assert clock.monotonic() == pytest.approx(11.75)
        assert clock.sleeps == [1.5, 0.25]

    def test_negative_sleep_raises_like_time_sleep(self) -> None:
        clock = FakeClock(5.0)
        with pytest.raises(ValueError):
            clock.sleep(-1.0)
        assert clock.monotonic() == 5.0

    def test_advance_rejects_negative_time(self) -> None:
        with pytest.raises(ValueError):
            FakeClock().advance(-0.1)

    def test_callbacks_fire_in_time_order_during_advance(self) -> None:
        clock = FakeClock()
        fired: list[tuple[str, float]] = []
        clock.call_at(2.0, lambda: fired.append(("b", clock.monotonic())))
        clock.call_later(1.0, lambda: fired.append(("a", clock.monotonic())))
        clock.call_at(5.0, lambda: fired.append(("late", clock.monotonic())))
        clock.advance(3.0)
        assert fired == [("a", 1.0), ("b", 2.0)]
        assert clock.monotonic() == 3.0

    def test_already_cancelled_sleep_returns_false_without_advancing(self) -> None:
        clock = FakeClock()
        cancel = threading.Event()
        cancel.set()
        assert clock.sleep(1.0, cancel) is False
        assert clock.monotonic() == 0.0
        assert clock.sleeps == [1.0]

    def test_cancel_during_sleep_stops_time_at_the_cancel_moment(self) -> None:
        clock = FakeClock()
        cancel = threading.Event()
        clock.call_at(0.4, cancel.set)
        assert clock.sleep(1.0, cancel) is False
        assert clock.monotonic() == pytest.approx(0.4)

    def test_concurrent_advancers_fire_callbacks_in_time_order(self) -> None:
        clock = FakeClock()
        fired: list[float] = []
        for when in [0.5 * i for i in range(1, 41)]:
            clock.call_at(when, lambda: fired.append(clock.monotonic()))

        def worker() -> None:
            for _ in range(10):
                clock.sleep(0.5)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        clock.advance(0.0)
        for thread in threads:
            thread.join(timeout=5.0)
        # 20 sleeps of 0.5 s on one serialized timeline: callbacks up to 10 s,
        # each exactly once and in time order whichever thread advanced.
        assert fired == [0.5 * i for i in range(1, 21)]
        assert clock.monotonic() == pytest.approx(10.0)

    def test_a_callback_may_sleep_on_the_same_clock(self) -> None:
        clock = FakeClock()
        clock.call_at(1.0, lambda: clock.sleep(0.25))
        clock.sleep(2.0)
        assert clock.sleeps == [2.0, 0.25]
        assert clock.monotonic() == pytest.approx(2.0)

    def test_a_worker_thread_never_blocks(self) -> None:
        clock = FakeClock()
        done = threading.Event()

        def worker() -> None:
            for _ in range(100):
                clock.sleep(10.0)  # 1000 s of fake time
            done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5.0)
        assert done.is_set()
        assert clock.monotonic() == pytest.approx(1000.0)


class TestManualMode:
    def test_sleeper_is_released_only_by_advance(self) -> None:
        clock = FakeClock(auto_advance=False)
        results: list[bool] = []
        thread = threading.Thread(target=lambda: results.append(clock.sleep(2.0)))
        thread.start()
        try:
            assert clock.wait_for_sleepers(1)
            clock.advance(1.999)  # just before: still blocked
            assert clock.blocked_sleepers == 1
            assert results == []
            clock.advance(0.001)  # exactly at
        finally:
            clock.advance(10.0)  # never leave a thread blocked on failure
            thread.join(timeout=5.0)
        assert results == [True]
        assert clock.blocked_sleepers == 0

    def test_cancel_releases_a_blocked_sleeper(self) -> None:
        clock = FakeClock(auto_advance=False)
        cancel = threading.Event()
        results: list[bool] = []
        thread = threading.Thread(target=lambda: results.append(clock.sleep(5.0, cancel)))
        thread.start()
        try:
            assert clock.wait_for_sleepers(1)
            cancel.set()
            clock.advance(0.0)  # wake it now
            thread.join(timeout=5.0)
        finally:
            clock.advance(10.0)
            thread.join(timeout=5.0)
        assert results == [False]
        assert clock.monotonic() == pytest.approx(10.0)

    def test_wait_for_sleepers_times_out_when_nobody_sleeps(self) -> None:
        assert FakeClock(auto_advance=False).wait_for_sleepers(1, timeout_s=0.01) is False


class TestSystemClock:
    def test_monotonic_moves_forward(self) -> None:
        clock = SystemClock()
        first = clock.monotonic()
        assert clock.monotonic() >= first

    def test_set_cancel_returns_false_without_waiting(self) -> None:
        cancel = threading.Event()
        cancel.set()
        start = SYSTEM_CLOCK.monotonic()
        assert SYSTEM_CLOCK.sleep(30.0, cancel) is False
        assert SYSTEM_CLOCK.monotonic() - start < 1.0

    def test_zero_sleep_returns_true(self) -> None:
        assert SYSTEM_CLOCK.sleep(0.0) is True
        assert SYSTEM_CLOCK.sleep(0.0, threading.Event()) is True

    def test_zero_sleep_still_yields_like_time_sleep(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[float] = []
        monkeypatch.setattr("time.sleep", calls.append)
        SYSTEM_CLOCK.sleep(0.0)
        assert calls == [0.0]

    @pytest.mark.parametrize("cancel", [None, threading.Event()])
    def test_negative_sleep_raises_like_time_sleep(self, cancel: threading.Event | None) -> None:
        with pytest.raises(ValueError):
            SYSTEM_CLOCK.sleep(-0.001, cancel)

    def test_short_uncancelled_sleep_completes(self) -> None:
        assert SYSTEM_CLOCK.sleep(0.001, threading.Event()) is True
