"""Home authority display -- OnStepAdapter >= 0.4.0 establishes it
automatically from live status; there is no manual operator confirmation
step anymore (0.3.5's "Confirm at home" button/workflow is gone)."""

from __future__ import annotations

import time
from collections.abc import Callable

from astrotool_core.onstep import OnStepMountParkAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.fake_onstep_indi_client import make_fake_onstep_indi_connection
from collimation_tool.ui.mount_park_panel import MountParkPanel


def test_a_port_without_the_concept_shows_no_confirm_button(qapp: object) -> None:
    panel = MountParkPanel(FakeMountPark())
    assert panel._confirm_home_button.isHidden()
    assert panel._home_confirmed() is None
    panel.stop()


def test_onstep_also_has_no_confirm_button_but_still_reports_live_home_status(
    qapp: object,
) -> None:
    """>= 0.4.0 has no `confirm_home()` (home authority is automatic), so
    the button auto-hides exactly like a port with no concept of it at all
    -- but `home_confirmed` still reflects OnStepAdapter's own live status,
    not a stale operator declaration."""
    connection, made = make_fake_onstep_indi_connection()
    adapter = OnStepMountParkAdapter(connection)
    panel = MountParkPanel(adapter)
    panel._connect_button.setChecked(True)

    assert panel._confirm_home_button.isHidden()
    assert adapter.home_confirmed is True  # fake defaults home_authority_established=True
    panel._poll_status()
    assert "home confirmed" in panel._status_label.text()

    made[0].home_authority_established = False
    panel._poll_status()
    assert "HOME NOT CONFIRMED" in panel._status_label.text()
    panel.stop()


class _SlowPark(FakeMountPark):
    """A port whose actions take a while (the real adapter drives the mount home)."""

    long_running_actions = True

    def __init__(self, delay_s: float, error: str | None = None) -> None:
        super().__init__(start_parked=True)
        self._delay_s = delay_s
        self._error = error

    def unpark(self) -> None:
        time.sleep(self._delay_s)
        if self._error is not None:
            raise RuntimeError(self._error)
        super().unpark()


def _wait(predicate: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_a_long_running_unpark_never_blocks_the_gui_thread(qapp: object) -> None:
    panel = MountParkPanel(_SlowPark(delay_s=0.6))
    panel._connect_button.setChecked(True)
    started = time.monotonic()
    panel._unpark_button.click()
    assert time.monotonic() - started < 0.3  # returned at once; the mount moves on a worker
    assert not panel._unpark_button.isEnabled() and not panel._park_button.isEnabled()
    panel._poll_status()
    assert "in progress" in panel._status_label.text()

    def settled() -> bool:
        panel._poll_status()
        return not panel._action_in_flight

    _wait(settled)
    assert "Unparked" in panel._status_label.text()
    panel.stop()


def test_a_failed_long_running_unpark_is_reported_and_recoverable(qapp: object) -> None:
    panel = MountParkPanel(_SlowPark(delay_s=0.05, error="home route timed out"))
    panel._connect_button.setChecked(True)
    panel._unpark_button.click()

    def settled() -> bool:
        panel._poll_status()
        return not panel._action_in_flight

    _wait(settled)
    assert "home route timed out" in panel._status_label.text()
    assert panel._unpark_button.isEnabled()  # can retry
    panel.stop()
