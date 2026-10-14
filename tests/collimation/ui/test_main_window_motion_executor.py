"""S6.8a (R3/R4/R8): MainWindow wires ONE mount-motion executor through every workflow that
moves the mount.

- (1) one in-flight mount motion app-wide: while Mount Align's GOTO runs, the Mount panel's
  Park is refused with the busy reason (before: it was sent and slewed between steps);
- (4) quit during a GOTO stops the mount lock-free BEFORE any panel disconnects (before: the
  Mount panel's tracking-off queued behind the GOTO and Mount Align's own stop never aborted);
- (3) fine-collimation Cancel calls its abort hook.

A blocking angular mount stands in for OnStepAdapter's blocking GOTO: `move_angular` returns
only once `abort()` arrives (bounded by a real-time safety timeout -- a thread barrier, not a
policy wait)."""

from __future__ import annotations

import threading
import time

import numpy as np
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.mount.port import AxisDirection, CommandResult, MountAxis
from astrotool_core.testing.fake_mount import FakeAngularMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.ui.fine_collimation_panel import FineCollimationPanel
from collimation_tool.ui.main_window import MainWindow
from PySide6.QtGui import QCloseEvent

_SAFETY_S = 5.0


class _BlockingGotoMount(FakeAngularMountAdapter):
    def __init__(self, calls: list[str]) -> None:
        super().__init__()
        self.calls = calls
        self.entered = threading.Event()
        self._released = threading.Event()

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        self.calls.append("goto")
        self.entered.set()
        self._released.wait(_SAFETY_S)
        return CommandResult(accepted=False, message="stopped by the user")

    def abort(self) -> None:
        self.calls.append("abort")
        self._released.set()

    def release(self) -> None:
        self._released.set()


def _window(calls: list[str]) -> tuple[MainWindow, FakeMountPark, _BlockingGotoMount]:
    image = np.full((60, 80), 100.0, dtype=np.float32)
    park = FakeMountPark(start_parked=False)
    mount = _BlockingGotoMount(calls)
    mount.connect()
    window = MainWindow(
        ReplayCamera.from_arrays([image], cycle=True),
        device_lister=lambda: [],
        mount=park,
        pulse_mount=mount,
    )
    return window, park, mount


def _start_mount_align_goto(
    window: MainWindow, park: FakeMountPark, mount: _BlockingGotoMount
) -> None:
    runner = window._test_move_panel._runner
    assert runner.submit_sequence(
        park, mount, [(MountAxis.AXIS1, AxisDirection.POSITIVE, 100, 60.0)], park_after=False
    )
    assert mount.entered.wait(_SAFETY_S), "the Mount Align GOTO never started"


def _wait_idle(window: MainWindow) -> None:
    deadline = time.monotonic() + _SAFETY_S
    while window._test_move_panel._runner.is_busy:
        assert time.monotonic() < deadline
        time.sleep(0.005)


def test_a_park_during_a_mount_align_goto_is_refused_with_the_busy_reason(qapp: object) -> None:
    calls: list[str] = []
    window, park, mount = _window(calls)
    _start_mount_align_goto(window, park, mount)
    try:
        window._mount_panel._on_park()

        assert park.park_count == 0, "a park slew was sent while Mount Align's GOTO ran"
        text = window._mount_panel._status_label.text()
        assert "mount busy" in text and "Mount Align" in text, text
    finally:
        mount.release()
        _wait_idle(window)
        window.closeEvent(QCloseEvent())


def test_quit_during_a_goto_aborts_the_mount_before_any_panel_disconnects(qapp: object) -> None:
    calls: list[str] = []
    window, park, mount = _window(calls)
    _start_mount_align_goto(window, park, mount)
    real_stop = window._mount_panel.stop

    def mount_panel_stop() -> None:
        calls.append("mount_panel_stop")
        real_stop()

    window._mount_panel.stop = mount_panel_stop  # type: ignore[method-assign]
    try:
        window.closeEvent(QCloseEvent())

        assert "abort" in calls, f"quit never stopped the running GOTO: {calls}"
        assert calls.index("abort") < calls.index("mount_panel_stop"), calls
    finally:
        mount.release()
        _wait_idle(window)


def test_fine_collimation_cancel_calls_the_abort_hook(qapp: object) -> None:
    aborts: list[int] = []
    panel = FineCollimationPanel(get_frame=lambda: None, abort_motion=lambda: aborts.append(1))
    panel._on_cancel_clicked()
    assert aborts == [1]
    panel.stop()
