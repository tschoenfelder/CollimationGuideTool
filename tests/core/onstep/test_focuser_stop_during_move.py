"""S6.8c-R7 (#49, F6): the S6.0c Stop pattern applied to the focuser.

OnStepAdapter 0.5.0's `IndiFocuser.move_absolute` blocks until the target is confirmed (up to
30 s, indi_focuser.py:78-135) and `OnStepFocuserAdapter` holds `operation_lock` for all of it.
`IndiFocuser.stop()` is built for concurrent use: it sets `_cancel_requested` without the
motion lock (indi_focuser.py:137-139), and the move's poll loop checks that flag on every
pass (:107-108). Stop must therefore not take `operation_lock` -- serialized behind the move it
should stop, it could only run once that move had ended.

On the #51 simulator with a manual FakeClock the move stays in flight for as long as the test
wants; `ObservableRLock` tells deterministically whether a thread queued behind the lock
("blocked") or got through ("done").
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from astrotool_core.onstep import OnStepFocuserAdapter
from astrotool_core.testing import (
    FocuserScenario,
    OnStepScenario,
    make_simulated_onstep_connection,
)
from astrotool_core.testing.sim_onstep import ObservableRLock, install_observable_operation_lock
from astrotool_core.timing import FakeClock


def _until(condition: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() >= deadline:
            return False
        threading.Event().wait(0.001)
    return True


def _finish(clock: FakeClock, *threads: threading.Thread) -> None:
    """Run fake time on until every worker is done. Real time only bounds it."""
    deadline = time.monotonic() + 5.0
    while any(t.is_alive() for t in threads):
        assert time.monotonic() < deadline, "workers never finished"
        clock.advance(30.0)
        threading.Event().wait(0.002)


def _move_in_flight(
    *, stop_confirm_latency_s: float = 0.0
) -> tuple[FakeClock, ObservableRLock, OnStepFocuserAdapter, Any, threading.Thread, list[Any]]:
    """A 1000-step move at 10 steps/s (100 s of fake time) blocked in flight."""
    clock = FakeClock(auto_advance=False)
    connection, made = make_simulated_onstep_connection(
        OnStepScenario(
            focuser=FocuserScenario(
                steps_per_s=10.0, stop_confirm_latency_s=stop_confirm_latency_s
            )
        ),
        clock=clock,
    )
    lock = install_observable_operation_lock(connection)
    focuser = OnStepFocuserAdapter(connection)
    focuser.connect()
    assert focuser.status().available
    results: list[Any] = []
    mover = threading.Thread(
        target=lambda: results.append(focuser.move_absolute(6000)), daemon=True
    )
    mover.start()
    assert clock.wait_for_sleepers(1), "the move never started"
    return clock, lock, focuser, made[0], mover, results


class TestFocuserStopDuringMove:
    def test_stop_does_not_wait_behind_the_running_move(self) -> None:
        """Stop runs on the GUI thread: it must neither queue behind the move's lock nor wait
        out OnStepAdapter's stop confirmation (2 s of fake time here, up to 5 s real)."""
        clock, lock, focuser, _client, mover, _ = _move_in_flight(stop_confirm_latency_s=2.0)
        stopper = threading.Thread(target=focuser.stop, daemon=True)
        stopper.start()
        stopper.join(1.0)  # real-time bound only; the barrier below decides
        try:
            assert lock.wait_until_blocked_or_done(stopper) == "done"
        finally:
            _finish(clock, mover, stopper)

    def test_stop_ends_the_running_move_early(self) -> None:
        clock, _lock, focuser, client, mover, results = _move_in_flight()
        stopper = threading.Thread(target=focuser.stop, daemon=True)
        stopper.start()
        try:
            # No fake time passes: only the Stop can end the 100 s move.
            assert _until(lambda: not mover.is_alive()), "the move ran on despite the Stop"
            assert client.focuser.stop_calls >= 1
            assert clock.monotonic() == 0.0
            assert results and not results[0].accepted
            assert client.focuser.position == 5000  # stopped where it was, not at 6000
        finally:
            _finish(clock, mover, stopper)

    def test_a_move_queued_behind_another_operation_is_cancelled_unsent(self) -> None:
        clock = FakeClock(auto_advance=False)
        connection, made = make_simulated_onstep_connection(OnStepScenario(), clock=clock)
        lock = install_observable_operation_lock(connection)
        focuser = OnStepFocuserAdapter(connection)
        focuser.connect()
        results: list[Any] = []
        with lock:  # another operation (e.g. a mount GOTO) holds the connection
            mover = threading.Thread(
                target=lambda: results.append(focuser.move_absolute(6000)), daemon=True
            )
            mover.start()
            assert lock.wait_until_blocked_or_done(mover) == "blocked"
            focuser.stop()
        mover.join(5.0)
        assert made[0].focuser.move_log == [], "a move queued before the Stop was still sent"
        assert results and not results[0].accepted

    def test_a_move_requested_after_the_stop_runs_normally(self) -> None:
        connection, made = make_simulated_onstep_connection(OnStepScenario(), clock=FakeClock())
        focuser = OnStepFocuserAdapter(connection)
        focuser.connect()
        focuser.stop()
        result = focuser.move_absolute(5100)
        assert result.accepted
        assert made[0].focuser.position == 5100

    def test_a_relative_jog_is_stopped_too(self) -> None:
        clock = FakeClock(auto_advance=False)
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(focuser=FocuserScenario(steps_per_s=1.0)), clock=clock
        )
        focuser = OnStepFocuserAdapter(connection)
        focuser.connect()
        mover = threading.Thread(target=lambda: focuser.move(200), daemon=True)
        mover.start()
        assert clock.wait_for_sleepers(1)
        stopper = threading.Thread(target=focuser.stop, daemon=True)
        stopper.start()
        try:
            assert _until(lambda: not mover.is_alive()), "the jog ran on despite the Stop"
            assert made[0].focuser.position == 5000
        finally:
            _finish(clock, mover, stopper)

    def test_disconnect_waits_for_the_stop_to_reach_the_controller(self) -> None:
        """Quit = stop() then disconnect(): the release must not close the client before the
        stop worker has delivered OnStep's abort."""
        clock, _lock, focuser, client, mover, _ = _move_in_flight(stop_confirm_latency_s=2.0)
        order: list[str] = []
        real_stop, real_close = client.focuser.stop, client.close

        def stop(**kwargs: float) -> bool:
            done = real_stop(**kwargs)
            order.append("stop confirmed")
            return bool(done)

        def close() -> None:
            order.append("client closed")
            real_close()

        client.focuser.stop = stop
        client.close = close
        def quit_app() -> None:
            focuser.stop()
            focuser.disconnect()

        quitter = threading.Thread(target=quit_app, daemon=True)
        quitter.start()
        try:
            _finish(clock, quitter, mover)
            assert order[:1] == ["stop confirmed"] and order[-1] == "client closed"
        finally:
            _finish(clock, mover, quitter)

    def test_the_blockers_diagnostic_does_not_wait_behind_the_move(self) -> None:
        clock, lock, focuser, _client, mover, _ = _move_in_flight()
        out: list[tuple[str, ...]] = []
        reader = threading.Thread(target=lambda: out.append(focuser.blockers()), daemon=True)
        reader.start()
        try:
            assert lock.wait_until_blocked_or_done(reader) == "done"
            assert out and out[0]  # says why it could not read, never an empty "all clear"
        finally:
            _finish(clock, mover, reader)
