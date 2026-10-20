"""S6.8c-R5 (movement-paths F3/R5): one always-visible mount Stop at main-window level.

Before: a park slew had no in-app Stop -- MountParkPanel has no Stop button and Mount Align's Stop
is enabled only while Mount Align itself is busy. Now the main window's mount Stop is enabled
exactly while the executor has a mount motion in flight (park, Mount Align, reacquisition) and
calls the executor's lock-free `abort()`; a park/unpark it interrupted says "stopped by the user".

The park runs on the production `OnStepMountParkAdapter` + `OnStepMountPulseAdapter` over ONE
simulated OnStep connection on a manual `FakeClock`: the simulated park slew blocks in
`clock.sleep(park_latency_s)` holding `operation_lock`, exactly where OnStepAdapter's own
`park()` blocks in its status wait. Every stop worker a test starts is joined before it ends.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.onstep import OnStepMountParkAdapter, OnStepMountPulseAdapter
from astrotool_core.testing import OnStepScenario, make_simulated_onstep_connection
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.sim_onstep import SimulatedOnStepIndiClient
from astrotool_core.timing import FakeClock
from collimation_tool.application.mount_motion import MOTION_BUSY
from collimation_tool.ui.main_window import MainWindow
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QPushButton

_SAFETY_S = 5.0
_PARK_SLEW_S = 60.0
#: The object name of the global mount Stop (looked up by name, so a missing button is an
#: assertion, not an AttributeError).
_STOP_NAME = "globalMountStop"


def _stop_button(window: MainWindow) -> QPushButton:
    button = window.findChild(QPushButton, _STOP_NAME)
    assert button is not None, "no always-visible mount Stop in the main window"
    return button


def _settle_events() -> None:
    """Deliver the queued executor notifications (posted from worker threads)."""
    for _ in range(3):
        QCoreApplication.processEvents()


def _wait(predicate: object, what: str) -> None:
    deadline = time.monotonic() + _SAFETY_S
    while not predicate():  # type: ignore[operator]
        assert time.monotonic() < deadline, what
        time.sleep(0.002)


def _join(thread: threading.Thread | None) -> None:
    if thread is not None:
        thread.join(_SAFETY_S)
        assert not thread.is_alive(), f"{thread.name} outlived the test"


def _onstep_window(
    *, mount_align_connected: bool
) -> tuple[MainWindow, FakeClock, SimulatedOnStepIndiClient, OnStepMountPulseAdapter]:
    clock = FakeClock(auto_advance=False)
    connection, made = make_simulated_onstep_connection(
        OnStepScenario(parked=False, tracking=False, park_latency_s=_PARK_SLEW_S), clock=clock
    )
    park = OnStepMountParkAdapter(connection)
    pulse = OnStepMountPulseAdapter(connection)
    image = np.full((60, 80), 100.0, dtype=np.float32)
    window = MainWindow(
        ReplayCamera.from_arrays([image], cycle=True),
        device_lister=lambda: [],
        mount=park,
        pulse_mount=pulse,
    )
    window._mount_panel._connect_button.setChecked(True)
    if mount_align_connected:
        window._test_move_panel._connect_button.setChecked(True)
    return window, clock, made[0], pulse


@pytest.mark.parametrize("mount_align_connected", [False, True])
def test_stop_during_a_park_slew_reaches_the_mount_at_once(
    qapp: object, mount_align_connected: bool
) -> None:
    window, clock, client, pulse = _onstep_window(mount_align_connected=mount_align_connected)
    panel = window._mount_panel
    connection = pulse._connection
    users_before = connection._users
    try:
        panel._on_park()
        assert clock.wait_for_sleepers(1, timeout_s=_SAFETY_S), "the park slew never started"
        _settle_events()
        stop = _stop_button(window)
        assert stop.isVisible() or not window.isVisible()
        assert stop.isEnabled(), "the mount Stop is not enabled during a park slew"

        stop.click()
        _join(pulse._stop_worker)

        # The slew is still blocked (no clock time passed): the stop reached the mount at once.
        assert client.emergency_stop_calls >= 1, "Stop during the park slew never reached the mount"
        assert clock.blocked_sleepers == 1
        assert connection._users == users_before, "Stop changed who holds the OnStep connection"
    finally:
        # OnStepAdapter's park() after an ABORT: never confirms the parked state (indi_home.py:
        # 111-133), modelled by the simulator's rejected park once the slew time is over.
        client.park_rejected = True
        clock.advance(_PARK_SLEW_S)
        _join(panel._action_worker)
        _join(pulse._stop_worker)
    panel._poll_status()
    _settle_events()
    text = panel._status_label.text()
    assert "stopped by the user" in text, text
    assert "failed" not in text, text
    assert not _stop_button(window).isEnabled(), "Stop stays enabled after the park ended"
    window.closeEvent(QCloseEvent())
    _join(pulse._stop_worker)


def _fake_window() -> MainWindow:
    image = np.full((60, 80), 100.0, dtype=np.float32)
    return MainWindow(
        ReplayCamera.from_arrays([image], cycle=True),
        device_lister=lambda: [],
        mount=FakeMountPark(start_parked=False),
    )


@pytest.mark.parametrize(
    "owner", ["the Mount panel's park", "Mount Align", "guide-assisted reacquisition"]
)
def test_stop_is_enabled_exactly_while_a_motion_is_in_flight(qapp: object, owner: str) -> None:
    """Every workflow claims the one executor; a claim from a worker thread enables the Stop
    (queued to the GUI thread), its release disables it again."""
    window = _fake_window()
    executor = window._motion_executor
    stop = _stop_button(window)
    _settle_events()
    assert not stop.isEnabled(), "the mount Stop is enabled while nothing moves"
    claimed: list[object] = []
    worker = threading.Thread(target=lambda: claimed.append(executor.claim(owner)))
    worker.start()
    _join(worker)
    claim = claimed[0]
    assert not isinstance(claim, str), claim
    _settle_events()
    assert stop.isEnabled(), f"the mount Stop is not enabled while {owner} moves"
    releaser = threading.Thread(target=lambda: executor.release(claim))  # type: ignore[arg-type]
    releaser.start()
    _join(releaser)
    _settle_events()
    assert not stop.isEnabled(), f"the mount Stop stays enabled after {owner} ended"
    window.closeEvent(QCloseEvent())


def test_stop_cancels_the_claim_in_flight_whoever_owns_it(qapp: object) -> None:
    window = _fake_window()
    executor = window._motion_executor
    claim = executor.claim("Mount Align")
    assert not isinstance(claim, str)
    _settle_events()
    _stop_button(window).click()
    assert claim.cancelled, "the mount Stop did not stop Mount Align's motion"
    executor.release(claim)
    window.closeEvent(QCloseEvent())


def test_stop_while_idle_is_harmless(qapp: object) -> None:
    window, clock, client, pulse = _onstep_window(mount_align_connected=True)
    try:
        window._on_mount_stop_clicked()
        _join(pulse._stop_worker)
        assert client.emergency_stop_calls == 0, "an idle Stop sent an emergency stop"
        claim = window._motion_executor.claim("Mount Align")
        assert not isinstance(claim, str) and not claim.cancelled, claim
        window._motion_executor.release(claim)
        assert not str(window._motion_executor.claim("x")).startswith(MOTION_BUSY)
    finally:
        window.closeEvent(QCloseEvent())
        _join(pulse._stop_worker)
