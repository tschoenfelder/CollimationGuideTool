"""#52 K01/K03 (S6.2): MountParkPanel's connection lifecycle over the production
`OnStepMountParkAdapter` + `OnStepConnection` on the simulated OnStep controller (fake time).

A failure injected in each phase -- connect (OnStepAdapter's real error shapes, incl. the
KeyError of a reply missing a field), the #44 tracking enforcement right after connect
(start), the first status read (wait), tracking off on quit (cancel: the emergency stop not
confirmed, OnStepAdapter#17) and the release (disconnect) -- is shown and logged once, never
raised; the shared OnStep connection is given back and the next Connect works.
"""

from __future__ import annotations

import pytest
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer, TrackingGate
from astrotool_core.onstep import OnStepConnection, OnStepMountParkAdapter
from astrotool_core.testing import (
    ConnectFailure,
    OnStepScenario,
    SimulatedOnStepIndiClient,
    make_simulated_onstep_connection,
)
from astrotool_core.timing import FakeClock
from collimation_tool.ui.mount_park_panel import MountParkPanel


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


def _panel(
    scenario: OnStepScenario | None = None,
    *,
    enforcer: TrackingEnforcer | None = None,
) -> tuple[
    MountParkPanel, OnStepMountParkAdapter, OnStepConnection, list[SimulatedOnStepIndiClient]
]:
    clock = FakeClock()
    connection, made = make_simulated_onstep_connection(scenario or OnStepScenario(), clock=clock)
    adapter = OnStepMountParkAdapter(connection)
    panel = MountParkPanel(adapter, tracking_enforcer=enforcer, clock=clock)
    return panel, adapter, connection, made


class TestConnectPhase:
    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("INDI property LX200 OnStep.CONNECTION did not update"),
            ConnectionError("OnStep INDI device is not connected"),
            KeyError("UTC"),
            OSError("[Errno 11001] getaddrinfo failed"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_the_failure_is_shown_logged_once_and_a_retry_connects(
        self, qapp: object, caplog: pytest.LogCaptureFixture, error: Exception
    ) -> None:
        panel, _, connection, made = _panel(OnStepScenario(connect_errors=[error]))

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(True)

        assert "Connect failed" in panel._status_label.text()
        assert not panel._connect_button.isChecked() and not panel._connected
        assert len(_warnings(caplog, str(error))) == 1
        assert connection.client is None
        assert made[0].close_calls == 1  # the failed client is closed, whatever it raised

        panel._connect_button.setChecked(True)
        assert panel._connected and connection.client is made[1]
        panel.stop()

    def test_a_keyerror_reason_names_what_failed(self, qapp: object) -> None:
        """str(KeyError('UTC')) is just "'UTC'" -- useless in a diagnostic bundle."""
        panel, *_ = _panel(OnStepScenario(connect_errors=[ConnectFailure(KeyError("UTC"))]))

        panel._connect_button.setChecked(True)

        assert panel.diagnostic_context()["last_connect_error"] == "KeyError: 'UTC'"
        assert "KeyError: 'UTC'" in panel._status_label.text()
        panel.stop()


class _RaisingEnforcer(TrackingEnforcer):
    """A tracking enforcement that raises on connect (S6.4 made `enforce` fail closed for
    device errors; this is the defensive case of anything else escaping it)."""

    def enforce(self, context: str, *, fresh_wait_s: float = 0.0) -> TrackingGate:
        if context == "connect":
            raise RuntimeError("tracking enforcement crashed")
        return super().enforce(context, fresh_wait_s=fresh_wait_s)


class TestStartAndWaitPhases:
    def test_a_raising_tracking_enforcement_on_connect_leaves_the_panel_disconnected(
        self, qapp: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = FakeClock()
        connection, _ = make_simulated_onstep_connection(OnStepScenario(), clock=clock)
        adapter = OnStepMountParkAdapter(connection)
        enforcer = _RaisingEnforcer(adapter, mode=OperatingMode.TERRESTRIAL, clock=clock)
        panel = MountParkPanel(adapter, tracking_enforcer=enforcer, clock=clock)

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(True)

        assert not panel._connected and not panel._connect_button.isChecked()
        assert panel._connect_button.text() == "Connect"
        assert "tracking enforcement crashed" in panel._status_label.text()
        assert connection.client is None
        assert len(_warnings(caplog, "tracking enforcement crashed")) == 1
        panel.stop()

    def test_a_status_read_failing_right_after_connect_releases_the_connection(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        panel, adapter, connection, _ = _panel()
        _fail_next(monkeypatch, adapter, "status", TimeoutError("mount status timed out"))

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(True)

        assert not panel._connected and not panel._connect_button.isChecked()
        assert "mount status timed out" in panel._status_label.text()
        assert connection.client is None
        assert len(_warnings(caplog, "mount status timed out")) == 1

        panel._connect_button.setChecked(True)
        assert panel._connected
        assert panel._status_label.text().startswith("Parked")
        panel.stop()


class TestCancelAndDisconnectPhases:
    def test_an_unconfirmed_stop_on_quit_still_releases_the_connection(
        self, qapp: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OnStepAdapter#17: the emergency stop's confirmation can fail -> stop_tracking raises
        RuntimeError. The release must still happen (it used to be skipped, holding the shared
        OnStep connection open past quit)."""
        panel, _, connection, made = _panel()
        panel._connect_button.setChecked(True)
        made[0].stop_confirms = False

        with caplog.at_level("WARNING"):
            panel.stop()  # must not raise

        assert made[0].emergency_stop_calls == 1  # tracking off was attempted first
        assert connection.client is None
        assert not panel._connected
        assert len(_warnings(caplog, "still reports motion")) == 1

    def test_a_failing_release_on_disconnect_is_shown_and_reconnect_works(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        panel, _, _, made = _panel()
        panel._connect_button.setChecked(True)
        _fail_next(monkeypatch, made[0], "close", OSError("socket already gone"))

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(False)  # must not raise

        assert not panel._connected
        assert "Disconnect failed" in panel._status_label.text()
        assert len(_warnings(caplog, "socket already gone")) == 1

        panel._connect_button.setChecked(True)
        assert panel._connected
        panel.stop()
