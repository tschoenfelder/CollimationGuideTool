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
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.mount import AxisResponse, CalibrationMatrix
from astrotool_core.mount.movement_sizing import SIDEREAL_ARCSEC_PER_S
from astrotool_core.mount.port import AxisDirection, CommandResult, MountAxis
from astrotool_core.onstep import OnStepMountPulseAdapter
from astrotool_core.registration.geometry import rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)
from astrotool_core.testing import OnStepScenario, make_simulated_onstep_connection
from astrotool_core.testing.fake_mount import FakeAngularMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.frame_factory import single_star_image
from astrotool_core.testing.sim_onstep import SimulatedOnStepIndiClient
from astrotool_core.timing import FakeClock
from collimation_tool.application.star_acquisition import FocusedStarAcquisition
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
        park, mount, [(MountAxis.AXIS1, AxisDirection.POSITIVE, 100, 60.0)]
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


# ---- R4 end to end: fine-collimation Cancel during a reacquisition GOTO -----------------
_MAIN_SHAPE = (80, 100)
_GUIDE_SHAPE = (300, 400)
_GUIDE_ARCSEC_PER_PX = 6.6


def _registration() -> tuple[OpticalPrior, CrossCameraRegistrationResult]:
    prior_main = OpticalPrior(
        name="main",
        sensor_width_px=_MAIN_SHAPE[1],
        sensor_height_px=_MAIN_SHAPE[0],
        pixel_scale_arcsec=1.0,
    )
    scale = 0.25
    polygon = rect_polygon(
        _MAIN_SHAPE[1] * scale, _MAIN_SHAPE[0] * scale, center=(200.0, 150.0), rotation_deg=0.0
    )
    registration = CrossCameraRegistrationResult(
        method=RegistrationMethod.TERRESTRIAL,
        status=RegistrationStatus.OK_OVERLAP,
        rotation_deg=0.0,
        scale=scale,
        polygon_a_in_b=polygon,
        confidence=0.9,
    )
    return prior_main, registration


def _guide_calibration(center_rate_x: float) -> CalibrationMatrix:
    probe_ms = 2000
    px = probe_ms * center_rate_x * SIDEREAL_ARCSEC_PER_S / 1000.0 / _GUIDE_ARCSEC_PER_PX
    responses = {}
    for axis in MountAxis:
        for direction in AxisDirection:
            signed = px if direction is AxisDirection.POSITIVE else -px
            dx, dy = (signed, 0.0) if axis is MountAxis.AXIS1 else (0.0, signed)
            responses[(axis, direction)] = AxisResponse(
                axis=axis,
                direction=direction,
                duration_ms=probe_ms,
                dx_px=dx,
                dy_px=dy,
                px_per_ms=px / probe_ms,
            )
    return CalibrationMatrix(responses=responses)


def test_fine_collimation_cancel_during_a_reacquisition_goto_stops_the_mount(
    qapp: object,
) -> None:
    """Before S6.8a Cancel only set the run's cancel flag: the reacquisition's GOTO ran to its
    end, the mount was never stopped, and the second axis of the plan could still be sent."""
    clock = FakeClock()
    connection, made = make_simulated_onstep_connection(
        OnStepScenario(parked=False, tracking=False), clock=clock
    )
    mount = OnStepMountPulseAdapter(connection)
    mount.connect()
    client: SimulatedOnStepIndiClient = made[0]
    ha0, dec0 = client.ha_deg, client.dec_deg
    start = (250.0, 160.0)

    def render() -> np.ndarray:
        x = start[0] + (client.ha_deg - ha0) * 3600.0 / _GUIDE_ARCSEC_PER_PX
        y = start[1] + (client.dec_deg - dec0) * 3600.0 / _GUIDE_ARCSEC_PER_PX
        return single_star_image(_GUIDE_SHAPE, x=x, y=y, peak=3000.0, sigma=2.0, background=100.0)

    def fresh(_reference: float, _timeout: float) -> FrameAcquisitionResult:
        return FrameAcquisitionResult(
            FrameAcquisitionStatus.OK, DeliveredFrame(render(), time.monotonic(), 0.01)
        )

    image = np.full((60, 80), 100.0, dtype=np.float32)
    window = MainWindow(
        ReplayCamera.from_arrays([image], cycle=True),
        device_lister=lambda: [],
        mount=FakeMountPark(start_parked=False),
        pulse_mount=mount,
    )
    prior_main, registration = _registration()
    window._last_calibration_result = registration
    window._last_prior_a = prior_main
    rate = window._mount_alignment_settings.calibration_center_rate_x
    window._test_move_panel.calibration_for = lambda _key: _guide_calibration(rate)  # type: ignore[method-assign,assignment]
    window._right_panel.latest_mono_frame = render  # type: ignore[method-assign]
    window._right_panel.wait_for_frame_after = fresh  # type: ignore[method-assign,assignment]
    acquisition = FocusedStarAcquisition(roi_size=(16, 12))
    acquisition.select(
        single_star_image(_MAIN_SHAPE, x=90.0, y=40.0, peak=3000.0, sigma=2.0, background=100.0)
    )
    # The operator presses Cancel while the first GOTO runs (simulated time 0.01 s into it).
    clock.call_later(0.01, window._fine_collimation_panel._on_cancel_clicked)
    try:
        window._reacquire_via_guide(acquisition, None)
    finally:
        worker = mount._stop_worker
        if worker is not None:
            worker.join(timeout=_SAFETY_S)

    assert client.axis_move_calls, "the reacquisition never started a GOTO"
    assert client.emergency_stop_calls >= 1, "Cancel did not stop the mount"
    assert len(client.axis_move_calls) == 1, client.axis_move_calls
    window.closeEvent(QCloseEvent())
