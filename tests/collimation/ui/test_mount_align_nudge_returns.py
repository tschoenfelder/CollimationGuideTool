"""USER DECISION (2026-10-10, S6.6b) + AGENTS.md "Manual movement versus measurement": a
manual RA/Dec nudge is a positioning action -- it is sent at once (no BEFORE capture), returns
as soon as the mount move completes, and issue #43's no-motion check runs afterwards in the
background on fresh frames, only adding a warning if nothing moved.

Before S6.6b the nudge waited for a stability-verified BEFORE capture before anything was sent
and kept the panel busy through a verified AFTER capture: with long exposures the controls
stayed disabled for tens of seconds after the mount had already stopped."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, MountAxis
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer
from astrotool_core.testing.fake_mount import FakeAngularMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.timing import FakeClock
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel
from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner

_SETTINGS = MountAlignmentSettings(
    settle_ms=0, frame_settle_ms=0, stability_sample_interval_s=0.0, stability_timeout_s=60.0
)


class _SlowCamera:
    """Every frame wait blocks until `release` is set (a long exposure)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.waits = 0
        self.frame = np.random.default_rng(1).random((60, 80)).astype(np.float32) * 1000.0

    def wait(self, _reference: float, timeout_s: float) -> FrameAcquisitionResult:
        self.waits += 1
        if not self.release.wait(min(timeout_s, 30.0)):
            return FrameAcquisitionResult(FrameAcquisitionStatus.TIMEOUT)
        return FrameAcquisitionResult(
            FrameAcquisitionStatus.OK, DeliveredFrame(self.frame, time.monotonic(), 0.01)
        )


def _panel(camera: _SlowCamera) -> tuple[MountTestMovePanel, FakeAngularMountAdapter]:
    mount = FakeAngularMountAdapter()
    park = FakeMountPark(start_parked=False)
    panel = MountTestMovePanel(
        mount,
        mount_park=park,
        tracking_enforcer=TrackingEnforcer(
            park, OperatingMode.TERRESTRIAL, settle_timeout_s=0, clock=FakeClock()
        ),
        get_left_frame=lambda: camera.frame,
        get_right_frame=lambda: camera.frame,
        wait_for_left_frame=camera.wait,
        wait_for_right_frame=camera.wait,
        settings=_SETTINGS,
        runner=MountTestMoveRunner(clock=FakeClock()),
        threaded_captures=True,
    )
    panel._connect_button.setChecked(True)
    return panel, mount


def _until(condition: object, panel: MountTestMovePanel, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():  # type: ignore[operator]
        assert time.monotonic() < deadline, "condition never reached"
        time.sleep(0.005)
        panel._poll()


def test_the_move_is_sent_at_once_without_waiting_for_a_before_capture(qapp: object) -> None:
    camera = _SlowCamera()  # no frame would arrive for a BEFORE capture
    panel, mount = _panel(camera)

    panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
    _until(lambda: mount.angular_log, panel)

    assert mount.angular_log[0][:2] == (MountAxis.AXIS1, AxisDirection.POSITIVE)
    camera.release.set()
    panel.stop()


def test_the_nudge_returns_when_the_move_completes_while_the_check_still_runs(
    qapp: object,
) -> None:
    camera = _SlowCamera()
    panel, mount = _panel(camera)

    panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
    _until(
        lambda: mount.angular_log and panel._pending is None and not panel._runner.is_busy,
        panel,
    )

    assert "done" in panel._result_label.text(), panel._result_label.text()
    assert panel._nudge_buttons["left"]["RA +"].isEnabled()  # the controls are back
    assert panel._run_calibration_button.isEnabled()
    assert not camera.release.is_set()  # ... although the fresh AFTER frames are still pending

    camera.release.set()
    _until(lambda: panel._nudge_check_job is None, panel)
    assert "done" in panel._result_label.text()  # the check adds to the text, never fails it
    assert panel._verified_reference is not None  # its AFTER is the next nudge's BEFORE
    panel.stop()


def test_a_new_move_abandons_a_running_check(qapp: object) -> None:
    camera = _SlowCamera()
    panel, mount = _panel(camera)
    panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
    _until(
        lambda: mount.angular_log and panel._pending is None and not panel._runner.is_busy,
        panel,
    )
    first_check = panel._nudge_check_job
    assert first_check is not None

    panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.NEGATIVE)

    assert first_check.cancel.is_set()  # it would have measured across the second move
    camera.release.set()
    panel.stop()


class _GatedPark(FakeMountPark):
    """Review C1: the nudge check's tracking read blocks (on the check's own thread) until
    released, and reports tracking in the WRONG state for Terrestrial -- before S6.6b's fix the
    check then 'repaired' it with stop_tracking, i.e. an emergency stop racing the next move."""

    def __init__(self) -> None:
        super().__init__(start_parked=False)
        self.in_check = threading.Event()
        self.release = threading.Event()
        self.commands: list[tuple[str, str]] = []

    def status(self) -> Any:  # noqa: ANN401
        status = super().status()
        if threading.current_thread().name == "mount-align-nudge-check":
            self.in_check.set()
            self.release.wait(10.0)
            return replace(status, tracking=True)
        return status

    def stop_tracking(self) -> None:
        self.commands.append((threading.current_thread().name, "stop_tracking"))
        super().stop_tracking()

    def start_tracking(self) -> None:
        self.commands.append((threading.current_thread().name, "start_tracking"))
        super().start_tracking()


def test_an_abandoned_check_never_sends_a_tracking_command(qapp: object) -> None:
    camera = _SlowCamera()
    camera.release.set()
    park = _GatedPark()
    mount = FakeAngularMountAdapter()
    panel = MountTestMovePanel(
        mount,
        mount_park=park,
        tracking_enforcer=TrackingEnforcer(
            park, OperatingMode.TERRESTRIAL, settle_timeout_s=0, clock=FakeClock()
        ),
        get_left_frame=lambda: camera.frame,
        get_right_frame=lambda: camera.frame,
        wait_for_left_frame=camera.wait,
        wait_for_right_frame=camera.wait,
        settings=_SETTINGS,
        runner=MountTestMoveRunner(clock=FakeClock()),
        threaded_captures=True,
    )
    panel._connect_button.setChecked(True)
    panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
    _until(park.in_check.is_set, panel)  # the check is in its tracking step
    first_check = panel._nudge_check_job
    assert first_check is not None

    panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.NEGATIVE)  # the next move
    park.release.set()
    assert first_check.done.wait(10.0)

    from_check = [c for c in park.commands if c[0] == "mount-align-nudge-check"]
    assert from_check == []  # verify only: repairing tracking is the move paths' job
    panel.stop()
