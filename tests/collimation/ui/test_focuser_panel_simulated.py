"""Issue #51: FocuserPanel over the production `OnStepFocuserAdapter` +
`OnStepConnection` on the simulated OnStep controller (fake time). Defect
regressions: connect-error variants (diagnostic 11e564ad, 7bb7b68) and a
focuser stuck Busy forever (diagnostic 73c7d59d, FocuserPanel half of 3fc09ae).

Seam note (#51 dependency): the panels time their confirmation safety net with
`time.monotonic()` directly (no clock seam), so stuck-Busy tests age the
issued-at timestamp the same way the existing panel tests do.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable

import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.onstep import (
    OnStepFocuserAdapter,
)
from astrotool_core.session import describe_failure
from astrotool_core.testing import (
    FocuserScenario,
    OnStepScenario,
    SimulatedOnStepIndiClient,
    make_simulated_onstep_connection,
)
from astrotool_core.timing import FakeClock
from collimation_tool.ui.focuser_panel import FocuserPanel


def _focuser_panel(
    scenario: OnStepScenario, clock: FakeClock
) -> tuple[FocuserPanel, list[SimulatedOnStepIndiClient]]:
    connection, made = make_simulated_onstep_connection(scenario, clock=clock)
    panel = FocuserPanel(
        OnStepFocuserAdapter(connection),
        get_frame=lambda: None,
        wait_for_frame=lambda _reference, _timeout: FrameAcquisitionResult(
            FrameAcquisitionStatus.TIMEOUT
        ),
        set_auto_exposure_paused=lambda _paused: None,
    )
    return panel, made


class TestConnectErrorVariantsAreShownNotRaised:
    """Defect class "connect timeout" (diagnostic 11e564ad, fixed by 7bb7b68):
    `OnStepConnection.acquire()` propagates TimeoutError/RuntimeError/
    ValueError as well as ConnectionError, and FocuserPanel used to catch only
    ConnectionError -- an unhandled TimeoutError on Connect. Here the real
    error shapes come from the simulated controller through the production
    connection and adapter, and the panel must recover once the fault clears."""

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("INDI property LX200 OnStep.CONNECTION did not update"),
            RuntimeError("INDI client is already connected; close before reconnecting"),
            ValueError("Invalid isoformat string: ''"),
            ConnectionError("OnStep INDI device is not connected"),
            KeyError("UTC"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_the_failure_is_shown_and_a_retry_connects(
        self, qapp: object, error: BaseException
    ) -> None:
        panel, _ = _focuser_panel(OnStepScenario(connect_errors=[error]), FakeClock())

        panel._connect_button.setChecked(True)

        assert "Connect failed" in panel._status_label.text()
        assert str(error) in panel._status_label.text()
        assert not panel._connect_button.isChecked()
        # S6.2: the one normalized reason (a bare str(KeyError) would be just "'UTC'").
        assert panel.diagnostic_context()["last_connect_error"] == describe_failure(error)

        panel._connect_button.setChecked(True)  # the controller answers now
        assert panel._status_label.text() == "Position 5000 / 10000"
        panel.stop()


class TestStuckBusyRecovers:
    """Defect class "stuck Busy" (diagnostic 73c7d59d, fixed by 3fc09ae): the
    device never leaves Busy, and the confirmation-timeout safety net used to
    be reachable only while NOT moving."""

    def test_a_focuser_stuck_busy_forever_releases_the_move_lock_after_the_timeout(
        self, qapp: object
    ) -> None:
        """The FocuserPanel half of 3fc09ae, which proofs/diag-73c7d59d lists
        as having no regression test. In/Out stay disabled while the driver
        says it moves (they also check is_moving()), but the panel's own
        in-flight lock must clear so Auto Focus is offered again."""
        clock = FakeClock()
        panel, made = _focuser_panel(
            OnStepScenario(focuser=FocuserScenario(busy_forever=True)), clock
        )
        panel._connect_button.setChecked(True)
        assert panel._auto_focus_button.isEnabled()

        panel._on_move_out()  # the simulated driver goes Busy and stays there
        assert _until(lambda: not panel._jog.is_busy)  # S6.8c: the jog runs off the GUI thread
        assert clock.monotonic() == 35.0  # 0.4.1: 30 s move timeout + 5 s stop wait, fake time
        panel._poll_status()
        assert panel._move_in_flight and not panel._auto_focus_button.isEnabled()

        assert panel._move_issued_at is not None
        panel._move_issued_at -= 999.0  # the panel's real-clock safety net elapses
        panel._poll_status()

        assert made[0].focuser.moving is True  # still stuck
        assert panel._move_in_flight is False
        assert panel._auto_focus_button.isEnabled()
        panel.stop()


def _fail_next(
    monkeypatch: pytest.MonkeyPatch, target: object, name: str, error: Exception
) -> None:
    """Make `target.name` raise `error` once, then behave normally again."""
    real = getattr(target, name)
    armed = [True]

    def once(*args: object, **kwargs: object) -> object:
        if armed[0]:
            armed[0] = False
            raise error
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, once)


def _warnings(caplog: pytest.LogCaptureFixture, text: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and text in r.getMessage()
    ]


class TestEveryConnectionPhaseLeavesThePanelRetryable:
    """#52 K01/K03 (S6.2): a failure injected in each lifecycle phase -- connect, the first
    status read (wait), the motion abort on quit (cancel) and the release (disconnect) -- is
    shown and logged once, never raised; the shared OnStep connection is given back and the
    next Connect works."""

    def test_connect_failure_is_logged_exactly_once(
        self, qapp: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        error = TimeoutError("INDI property LX200 OnStep.CONNECTION did not update")
        panel, _ = _focuser_panel(OnStepScenario(connect_errors=[error]), FakeClock())

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(True)

        assert len(_warnings(caplog, "CONNECTION did not update")) == 1
        panel.stop()

    def test_a_status_read_failing_right_after_connect_releases_the_connection(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        connection, _ = make_simulated_onstep_connection(OnStepScenario(), clock=FakeClock())
        adapter = OnStepFocuserAdapter(connection)
        panel = FocuserPanel(adapter)
        _fail_next(monkeypatch, adapter, "status", TimeoutError("focuser status timed out"))

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(True)

        assert "Connect failed" in panel._status_label.text()
        assert "focuser status timed out" in panel._status_label.text()
        assert not panel._connect_button.isChecked()
        assert not panel._connected
        assert connection.client is None  # released: nothing half-open
        assert len(_warnings(caplog, "focuser status timed out")) == 1
        assert panel.diagnostic_context()["last_connect_error"] == "focuser status timed out"

        panel._connect_button.setChecked(True)  # retry
        assert panel._connected
        assert panel._status_label.text() == "Position 5000 / 10000"
        panel.stop()

    def test_a_failing_abort_on_quit_still_releases_the_connection(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        connection, _ = make_simulated_onstep_connection(OnStepScenario(), clock=FakeClock())
        adapter = OnStepFocuserAdapter(connection)
        panel = FocuserPanel(adapter)
        panel._connect_button.setChecked(True)
        _fail_next(monkeypatch, adapter, "stop", RuntimeError("focuser abort not confirmed"))

        with caplog.at_level("WARNING"):
            panel.stop()  # must not raise

        assert connection.client is None
        assert not panel._connected
        assert len(_warnings(caplog, "focuser abort not confirmed")) == 1

        panel._connect_button.setChecked(False)
        panel._connect_button.setChecked(True)  # retryable
        assert panel._connected and connection.client is not None
        panel.stop()

    def test_a_failing_release_on_disconnect_is_shown_and_reconnect_works(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        connection, made = make_simulated_onstep_connection(OnStepScenario(), clock=FakeClock())
        panel = FocuserPanel(OnStepFocuserAdapter(connection))
        panel._connect_button.setChecked(True)
        _fail_next(monkeypatch, made[0], "close", OSError("socket already gone"))

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(False)  # must not raise

        assert not panel._connected
        assert "Disconnect failed" in panel._status_label.text()
        assert len(_warnings(caplog, "socket already gone")) == 1

        panel._connect_button.setChecked(True)
        assert panel._connected
        assert panel._status_label.text() == "Position 5000 / 10000"
        panel.stop()

    def test_an_internal_error_in_the_abort_on_quit_reaches_the_incident_hook(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hook: list[type[BaseException]] = []
        monkeypatch.setattr(sys, "excepthook", lambda t, _v, _tb: hook.append(t))
        connection, _ = make_simulated_onstep_connection(OnStepScenario(), clock=FakeClock())
        adapter = OnStepFocuserAdapter(connection)
        panel = FocuserPanel(adapter)
        panel._connect_button.setChecked(True)
        _fail_next(monkeypatch, adapter, "stop", AttributeError("'NoneType' has no 'stop'"))

        panel.stop()  # must not raise

        assert hook == [AttributeError]
        assert connection.client is None  # the release still ran


def _until(condition: Callable[[], bool], timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() >= deadline:
            return False
        threading.Event().wait(0.001)
    return True


class _Watchdog:
    """Before S6.8c the jog's blocking move ran on the GUI thread, which on a manual FakeClock
    never returns. After a real-time bound this moves fake time on (the move then ends), so
    such a run FAILS on the assertion after the click instead of hanging the test session."""

    def __init__(self, clock: FakeClock, after_s: float = 2.0) -> None:
        self._cancelled = threading.Event()

        def fire() -> None:
            if not self._cancelled.wait(after_s):
                clock.advance(1000.0)

        threading.Thread(target=fire, daemon=True).start()

    def cancel(self) -> None:
        self._cancelled.set()


def _jog_in_flight() -> tuple[FocuserPanel, list[SimulatedOnStepIndiClient], FakeClock]:
    """A 200-step Out jog at 1 step/s (200 s of fake time) clicked on the GUI thread."""
    clock = FakeClock(auto_advance=False)
    panel, made = _focuser_panel(
        OnStepScenario(focuser=FocuserScenario(steps_per_s=1.0)), clock
    )
    panel._connect_button.setChecked(True)
    button = panel._step_group.button(200)
    assert button is not None
    button.setChecked(True)
    watchdog = _Watchdog(clock)
    panel._on_move_out()
    watchdog.cancel()
    assert clock.monotonic() == 0.0, "the click returned only once the move had ended"
    assert clock.wait_for_sleepers(1), "the jog's move never started"
    return panel, made, clock


class TestJogRunsOffTheGuiThread:
    """S6.8c (R7, F6): OnStepAdapter's focuser move blocks until the target is confirmed (up
    to 30 s). The jog ran it on the GUI thread -- the window froze for the travel time and
    its own Stop could not be clicked."""

    def test_the_blocking_move_never_runs_on_the_gui_thread(self, qapp: object) -> None:
        panel, made = _focuser_panel(OnStepScenario(), FakeClock())
        panel._connect_button.setChecked(True)
        callers: list[int] = []
        real_move = made[0].focuser.move_absolute

        def spy(target: int, **kwargs: float) -> object:
            callers.append(threading.get_ident())
            return real_move(target, **kwargs)

        made[0].focuser.move_absolute = spy  # type: ignore[method-assign]
        panel._on_move_out()

        assert _until(lambda: made[0].focuser.position == 5001)
        assert callers and callers[0] != threading.get_ident(), (
            "the jog's blocking move ran on the GUI thread"
        )
        panel.stop()

    def test_the_panel_stays_responsive_during_the_jog(self, qapp: object) -> None:
        panel, _made, clock = _jog_in_flight()
        try:
            panel._poll_status()  # a GUI-thread poll during the move returns at once
            assert "(moving" in panel._status_label.text()
            assert not panel._in_button.isEnabled() and not panel._out_button.isEnabled()
            assert panel._stop_button.isEnabled()
        finally:
            panel.stop()
            clock.advance(1000.0)

    def test_stop_during_the_jog_ends_the_move_early(self, qapp: object) -> None:
        panel, made, clock = _jog_in_flight()
        panel._on_stop()  # on the GUI thread; must return while the move runs

        assert _until(lambda: not panel._jog.is_busy), "the jog ran on despite the Stop"
        assert made[0].focuser.stop_calls >= 1
        assert made[0].focuser.position == 5000  # stopped short of 5200, no fake time passed
        assert clock.monotonic() == 0.0
        panel._poll_status()
        assert panel._out_button.isEnabled()  # the next jog is offered again
        panel.stop()

    def test_quit_during_the_jog_stops_it(self, qapp: object) -> None:
        panel, made, clock = _jog_in_flight()
        panel.stop()  # app quit: abort, then release

        assert _until(lambda: not panel._jog.is_busy), "the jog ran on after quit"
        assert made[0].focuser.stop_calls >= 1
        assert made[0].focuser.position == 5000
        assert clock.monotonic() == 0.0
