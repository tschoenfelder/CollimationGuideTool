"""The operator's explicit "Confirm at home" step (OnStepAdapter refuses all motion until then)."""

from __future__ import annotations

import time
from collections.abc import Callable

from astrotool_core.onstep import OnStepMountParkAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.fake_onstep_client import make_fake_onstep_connection
from collimation_tool.ui.mount_park_panel import MountParkPanel


def test_a_port_without_the_concept_shows_no_confirm_button(qapp: object) -> None:
    panel = MountParkPanel(FakeMountPark())
    assert panel._confirm_home_button.isHidden()
    assert panel._home_confirmed() is None
    panel.stop()


def test_the_status_says_home_is_not_confirmed_until_the_operator_confirms(
    qapp: object,
) -> None:
    connection = make_fake_onstep_connection()
    adapter = OnStepMountParkAdapter(connection)
    panel = MountParkPanel(adapter)
    panel._connect_button.setChecked(True)
    assert not panel._confirm_home_button.isHidden()
    assert "HOME NOT CONFIRMED" in panel._status_label.text()

    panel._confirm_home_button.click()

    assert "Home position confirmed" in panel._status_label.text()
    panel._poll_status()
    assert "home confirmed" in panel._status_label.text()
    assert adapter.home_confirmed
    panel.stop()


def test_a_failed_confirmation_is_shown_not_swallowed(qapp: object) -> None:
    adapter = OnStepMountParkAdapter(make_fake_onstep_connection())
    panel = MountParkPanel(adapter)  # never connected: the adapter cannot confirm
    panel._on_confirm_home()
    assert "Confirm home failed" in panel._status_label.text()
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
