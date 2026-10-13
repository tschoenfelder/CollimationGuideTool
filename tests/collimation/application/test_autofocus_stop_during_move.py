"""S6.8c (R7, F-b/F-c): FocuserPanel's Stop during an autofocus run cancels the runner and
then calls `OnStepFocuserAdapter.stop()`. Before S6.8c that stop queued behind the running
autofocus move (the move ran to its end, up to 30 s); now it interrupts it and the run ends
without sending another move.

Known wording gap (recorded in proofs/49-focuser-stop.toml): `BoundedFocusSearcher._move_to`
checks the cancel flag only between moves, so a run whose move a Stop interrupted reports
MOVE_REJECTED ("the focuser rejected the commanded move"), not CANCELLED.
"""

from __future__ import annotations

import threading
import time

from astrotool_core.onstep import OnStepFocuserAdapter
from astrotool_core.testing import (
    FocuserScenario,
    OnStepScenario,
    make_simulated_onstep_connection,
)
from astrotool_core.timing import FakeClock
from collimation_tool.application.autofocus_search import (
    AutofocusStatus,
    BoundedFocusSearcher,
    BoundedSearchResult,
    FocusSample,
)


def test_a_stop_during_an_autofocus_move_ends_the_run_without_another_move() -> None:
    focuser_clock = FakeClock(auto_advance=False)  # the 250-step probe move takes 25 s
    connection, made = make_simulated_onstep_connection(
        OnStepScenario(focuser=FocuserScenario(steps_per_s=10.0)), clock=focuser_clock
    )
    focuser = OnStepFocuserAdapter(connection)
    focuser.connect()
    cancel = threading.Event()
    searcher = BoundedFocusSearcher(focuser, higher_is_better=False, clock=FakeClock())
    results: list[BoundedSearchResult] = []
    run = threading.Thread(
        target=lambda: results.append(
            searcher.search(lambda: FocusSample(3.0), cancel_check=cancel.is_set)
        ),
        daemon=True,
    )
    run.start()
    assert focuser_clock.wait_for_sleepers(1), "the first probe move never started"

    # FocuserPanel._on_stop: the runner's cancel, then the focuser stop (GUI thread).
    cancel.set()
    stopper = threading.Thread(target=focuser.stop, daemon=True)
    stopper.start()
    run.join(3.0)
    try:
        assert not run.is_alive(), "the autofocus move ran on despite the Stop"
        assert made[0].focuser.move_log == [5250]  # no move after the Stop
        assert made[0].focuser.position == 5000
        assert results[0].status in (AutofocusStatus.CANCELLED, AutofocusStatus.MOVE_REJECTED)
    finally:
        deadline = time.monotonic() + 5.0
        while (run.is_alive() or stopper.is_alive()) and time.monotonic() < deadline:
            focuser_clock.advance(30.0)
            threading.Event().wait(0.002)
