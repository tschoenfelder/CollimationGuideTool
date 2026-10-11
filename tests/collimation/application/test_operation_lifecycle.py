"""S6.3 (#52 L01/L02/L04): the small operation-lifecycle helpers.

- `OperationLifecycle`: one background operation at a time with a GUARANTEED terminal
  state (succeeded / failed / cancelled) and published outcome -- whatever the work does.
- `completion_verdict`: the one "seen-Busy then idle, or a safety-net timeout" rule of the
  one-action-at-a-time panels (exact boundaries: `elapsed > timeout`).
- `StatusPollGuard`: a periodic status poll never raises into Qt; a failure streak is
  logged once, an internal error reaches the #10 incident hook once.
"""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from types import TracebackType

import pytest
from collimation_tool.application.operation_lifecycle import (
    CompletionVerdict,
    OperationLifecycle,
    OperationState,
    StatusPollGuard,
    completion_verdict,
)


def _inline(work: Callable[[], None]) -> None:
    work()


class TestOperationLifecycle:
    def test_a_successful_run_publishes_once_and_ends_succeeded(self) -> None:
        lifecycle: OperationLifecycle[str] = OperationLifecycle("test-op")
        published: list[str] = []
        before = lifecycle.state
        assert lifecycle.start(
            lambda: "done", on_error=lambda exc: "failed", publish=published.append,
            spawn=_inline,
        )
        assert published == ["done"]
        assert (before, lifecycle.state) == (OperationState.IDLE, OperationState.SUCCEEDED)
        assert not lifecycle.is_busy

    def test_a_raising_run_publishes_the_error_outcome_and_ends_failed(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        lifecycle: OperationLifecycle[str] = OperationLifecycle("test-op")
        published: list[str] = []

        def work() -> str:
            raise TimeoutError("INDI did not answer")

        with caplog.at_level(logging.ERROR):
            lifecycle.start(
                work, on_error=lambda exc: f"failed: {exc}", publish=published.append,
                spawn=_inline,
            )
        assert published == ["failed: INDI did not answer"]
        assert lifecycle.state is OperationState.FAILED
        assert not lifecycle.is_busy
        assert any(
            "test-op" in r.getMessage() and r.exc_info is not None for r in caplog.records
        )

    def test_a_raising_error_handler_still_yields_a_failed_outcome(self) -> None:
        """S6.3 review: the UI must still get a take_latest() result -- e.g. an exception
        whose str() raises breaks the runner's own failure text once."""

        class Unprintable(Exception):
            def __str__(self) -> str:
                raise ValueError("cannot format")

        lifecycle: OperationLifecycle[str] = OperationLifecycle("test-op")
        published: list[str] = []

        def work() -> str:
            raise Unprintable()

        def on_error(exc: Exception) -> str:
            return f"failed: {exc}"

        lifecycle.start(work, on_error=on_error, publish=published.append, spawn=_inline)
        assert published == [
            "failed: Unprintable (building its failure outcome raised ValueError)"
        ]
        assert lifecycle.state is OperationState.FAILED
        assert not lifecycle.is_busy

    def test_an_error_handler_that_always_raises_cannot_strand_busy(self) -> None:
        lifecycle: OperationLifecycle[str] = OperationLifecycle("test-op")
        published: list[str] = []

        def work() -> str:
            raise RuntimeError("first")

        def on_error(exc: Exception) -> str:
            raise ValueError("second")

        lifecycle.start(work, on_error=on_error, publish=published.append, spawn=_inline)
        assert published == []
        assert lifecycle.state is OperationState.FAILED
        assert not lifecycle.is_busy

    def test_a_cancel_during_the_run_ends_cancelled_and_a_new_start_rearms(self) -> None:
        lifecycle: OperationLifecycle[str] = OperationLifecycle("test-op")
        published: list[str] = []

        def work() -> str:
            lifecycle.cancel()
            assert lifecycle.cancel_event.is_set() and lifecycle.is_cancel_requested()
            return "stopped"

        lifecycle.start(work, on_error=str, publish=published.append, spawn=_inline)
        states = [lifecycle.state]
        seen: list[bool] = []

        def second() -> str:
            seen.append(lifecycle.is_cancel_requested())
            return "second"

        lifecycle.start(second, on_error=str, publish=published.append, spawn=_inline)
        states.append(lifecycle.state)
        assert seen == [False]  # the next operation starts un-cancelled
        assert states == [OperationState.CANCELLED, OperationState.SUCCEEDED]
        assert published == ["stopped", "second"]

    def test_one_at_a_time(self) -> None:
        lifecycle: OperationLifecycle[str] = OperationLifecycle("test-op")
        assert lifecycle.try_begin()
        assert lifecycle.is_busy and lifecycle.state is OperationState.RUNNING
        ran: list[str] = []

        def work() -> str:
            ran.append("work")
            return "x"

        assert not lifecycle.start(work, on_error=str, publish=ran.append, spawn=_inline)
        assert not lifecycle.try_begin()
        assert ran == []

    def test_on_begin_runs_under_the_lock_only_for_an_accepted_start(self) -> None:
        lifecycle: OperationLifecycle[str] = OperationLifecycle("test-op")
        begun: list[bool] = []
        assert lifecycle.try_begin(on_begin=lambda: begun.append(lifecycle.lock.locked()))
        assert begun == [True]
        assert not lifecycle.try_begin(on_begin=lambda: begun.append(True))
        assert begun == [True]

    def test_the_default_spawn_is_a_named_daemon_thread(self) -> None:
        lifecycle: OperationLifecycle[str] = OperationLifecycle("test-op")
        done = threading.Event()
        names: list[tuple[str, bool]] = []

        def work() -> str:
            current = threading.current_thread()
            names.append((current.name, current.daemon))
            return "ok"

        lifecycle.start(
            work, on_error=str, publish=lambda _o: done.set(), thread_name="my-worker"
        )
        assert done.wait(5.0)
        assert names == [("my-worker", True)]


class TestCompletionVerdict:
    @pytest.mark.parametrize(
        ("elapsed", "verdict"),
        [
            (9.99, CompletionVerdict.PENDING),
            (10.0, CompletionVerdict.PENDING),
            (10.01, CompletionVerdict.TIMED_OUT),
        ],
        ids=["before", "at", "after"],
    )
    def test_the_safety_net_boundary(self, elapsed: float, verdict: CompletionVerdict) -> None:
        seen, result = completion_verdict(
            busy=True, seen_busy=True, issued_at=100.0, now=100.0 + elapsed, timeout_s=10.0
        )
        assert seen is True and result is verdict

    def test_the_timeout_fires_even_while_busy_the_3fc09ae_rule(self) -> None:
        assert completion_verdict(
            busy=True, seen_busy=False, issued_at=0.0, now=31.0, timeout_s=30.0
        ) == (True, CompletionVerdict.TIMED_OUT)

    def test_idle_after_busy_confirms_and_wins_over_the_timeout(self) -> None:
        assert completion_verdict(
            busy=False, seen_busy=True, issued_at=0.0, now=99.0, timeout_s=10.0
        ) == (True, CompletionVerdict.CONFIRMED)

    def test_idle_before_any_busy_is_not_a_confirmation(self) -> None:
        assert completion_verdict(
            busy=False, seen_busy=False, issued_at=0.0, now=1.0, timeout_s=10.0
        ) == (False, CompletionVerdict.PENDING)

    def test_an_unknown_reading_only_lets_the_timeout_decide(self) -> None:
        assert completion_verdict(
            busy=None, seen_busy=True, issued_at=0.0, now=1.0, timeout_s=10.0
        ) == (True, CompletionVerdict.PENDING)
        assert completion_verdict(
            busy=None, seen_busy=False, issued_at=0.0, now=10.5, timeout_s=10.0
        ) == (False, CompletionVerdict.TIMED_OUT)

    def test_no_issue_time_never_times_out(self) -> None:
        assert completion_verdict(
            busy=True, seen_busy=True, issued_at=None, now=1e9, timeout_s=10.0
        ) == (True, CompletionVerdict.PENDING)


class TestStatusPollGuard:
    def test_success_returns_none(self) -> None:
        assert StatusPollGuard("Focuser").run(lambda: None) is None

    def test_a_failure_streak_is_logged_once_and_its_recovery_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        guard = StatusPollGuard("Focuser")

        def broken() -> None:
            raise KeyError("UTC")

        with caplog.at_level(logging.INFO):
            first = guard.run(broken)
            second = guard.run(broken)
            assert guard.run(lambda: None) is None
            guard.run(broken)
        assert first is not None and first == second
        assert "KeyError" in first and "UTC" in first
        messages = [r.getMessage() for r in caplog.records]
        assert sum("status read failed" in m for m in messages) == 2  # two streaks
        assert sum("status read recovered" in m for m in messages) == 1
        assert guard.failing

    def test_an_internal_error_reaches_the_incident_hook_once_per_streak(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hooked: list[type[BaseException]] = []

        def hook(
            kind: type[BaseException], exc: BaseException, tb: TracebackType | None
        ) -> None:
            hooked.append(kind)

        monkeypatch.setattr(sys, "excepthook", hook)
        guard = StatusPollGuard("Filter Wheel")

        def bug() -> None:
            raise AttributeError("no such attribute")

        text = guard.run(bug)
        guard.run(bug)
        assert hooked == [AttributeError]
        assert text is not None and text.startswith("Internal error")

    def test_a_base_exception_is_never_swallowed(self) -> None:
        def interrupted() -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            StatusPollGuard("Mount").run(interrupted)
