"""Issue #51: FilterWheelPanel over the production `IndiFilterWheelAdapter`
on the simulated INDI filter wheel (fake time). Defect regression: a wheel
stuck Busy forever (diagnostic 73c7d59d, FilterWheelPanel half of 3fc09ae).

Seam note (#51 dependency): the panels time their confirmation safety net with
`time.monotonic()` directly (no clock seam), so stuck-Busy tests age the
issued-at timestamp the same way the existing panel tests do.
"""

from __future__ import annotations

from typing import cast

import pytest
from astrotool_core.testing import (
    FilterWheelScenario,
    SimulatedIndiClient,
    make_simulated_filter_wheel_adapter,
)
from astrotool_core.timing import FakeClock
from collimation_tool.ui.filter_wheel_panel import FilterWheelPanel


class TestStuckBusyRecovers:
    """Defect class "stuck Busy" (diagnostic 73c7d59d, fixed by 3fc09ae): the
    device never leaves Busy, and the confirmation-timeout safety net used to
    be reachable only while NOT moving."""

    def test_a_filter_wheel_stuck_busy_forever_re_enables_the_selector(self, qapp: object) -> None:
        clock = FakeClock()
        adapter, wheel = make_simulated_filter_wheel_adapter(
            FilterWheelScenario("ToupTek EFW 2", slot_names=("L", "R", "G"), busy_forever=True),
            clock=clock,
        )
        panel = FilterWheelPanel(adapter, selectable=True)
        panel._connect_button.setChecked(True)
        panel._slot_combo.setCurrentIndex(2)
        panel._on_set_clicked()
        clock.advance(600.0)
        panel._poll_status()
        assert wheel.commanded_slots == [3] and wheel.state == "Busy"
        assert not panel._slot_combo.isEnabled()

        assert panel._slot_change_issued_at is not None
        panel._slot_change_issued_at -= 999.0
        panel._poll_status()

        assert panel._slot_combo.isEnabled()
        assert panel._set_button.isEnabled()
        panel._connect_button.setChecked(False)


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
    """#52 K01 (S6.2): FilterWheelPanel caught a connect failure but neither logged it nor kept
    it for diagnostics -- the "shown-and-lost" defect ca5bb92 fixed in the other panels. And a
    failure in the first status read or the release escaped unguarded (K03)."""

    def test_an_unreachable_indiserver_is_shown_logged_once_and_kept_for_diagnostics(
        self, qapp: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter, _ = make_simulated_filter_wheel_adapter(
            FilterWheelScenario("ToupTek EFW 2", server_reachable=False), clock=FakeClock()
        )
        panel = FilterWheelPanel(adapter, selectable=True)

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(True)

        assert "connect failed" in panel._status_label.text().lower()
        assert not panel._connect_button.isChecked() and not panel.connected
        assert len(_warnings(caplog, "could not connect to indiserver")) == 1
        reason = panel.diagnostic_context()["last_connect_error"]
        assert reason is not None and "could not connect to indiserver" in reason

        cast(SimulatedIndiClient, adapter._client)._server_reachable = True  # indiserver is up
        panel._connect_button.setChecked(True)
        assert panel.connected
        assert panel.diagnostic_context()["last_connect_error"] is None
        panel.stop()

    def test_a_wrong_device_name_times_out_on_fake_time_and_a_retry_still_works(
        self, qapp: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = FakeClock()
        adapter, _ = make_simulated_filter_wheel_adapter(
            FilterWheelScenario("ToupTek EFW 2"), clock=clock, device_name="ToupTek EFW 9"
        )
        panel = FilterWheelPanel(adapter, selectable=True)

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(True)

        assert clock.monotonic() >= 10.0  # the adapter's CONNECTION wait, no real time
        assert not panel.connected
        assert len(_warnings(caplog, "did not confirm CONNECTION")) == 1
        assert "did not confirm CONNECTION" in panel.diagnostic_context()["last_connect_error"]

    def test_a_status_read_failing_right_after_connect_releases_the_client(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter, _ = make_simulated_filter_wheel_adapter(
            FilterWheelScenario("ToupTek EFW 2", slot_names=("L", "R", "G")), clock=FakeClock()
        )
        panel = FilterWheelPanel(adapter, selectable=True)
        _fail_next(monkeypatch, adapter, "status", ConnectionError("wheel status lost"))

        with caplog.at_level("WARNING"):
            panel._connect_button.setChecked(True)

        assert not panel.connected and not panel._connect_button.isChecked()
        assert "wheel status lost" in panel._status_label.text()
        assert not adapter.is_available  # released: the adapter is disconnected again
        assert len(_warnings(caplog, "wheel status lost")) == 1

        panel._connect_button.setChecked(True)
        assert panel.connected
        assert panel._status_label.text() == "Filter: 1 — L"
        panel.stop()

    def test_a_failing_release_on_quit_does_not_raise(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        adapter, _ = make_simulated_filter_wheel_adapter(
            FilterWheelScenario("ToupTek EFW 2"), clock=FakeClock()
        )
        panel = FilterWheelPanel(adapter)
        panel._connect_button.setChecked(True)
        _fail_next(monkeypatch, adapter, "disconnect", OSError("socket already gone"))

        with caplog.at_level("WARNING"):
            panel.stop()  # must not raise

        assert not panel.connected
        assert len(_warnings(caplog, "socket already gone")) == 1
