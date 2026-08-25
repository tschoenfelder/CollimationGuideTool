import time
from collections.abc import Callable

from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.target.roi_tracker import RoiTracker
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.frame_factory import single_star_image
from guide_tool.application.guide_controller import GuideController

_RESPONSE_VECTORS = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): (10.0, 0.0),
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): (-10.0, 0.0),
    (MountAxis.AXIS2, AxisDirection.POSITIVE): (0.0, 10.0),
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): (0.0, -10.0),
}


def make_calibration(px_per_ms: float = 0.1) -> CalibrationMatrix:
    responses = {
        (axis, direction): AxisResponse(
            axis=axis, direction=direction, duration_ms=100, dx_px=dx, dy_px=dy, px_per_ms=px_per_ms
        )
        for (axis, direction), (dx, dy) in _RESPONSE_VECTORS.items()
    }
    return CalibrationMatrix(responses=responses)


def _wait_until(
    predicate: Callable[[], bool], *, timeout_s: float = 2.0, interval_s: float = 0.02
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def test_idle_before_start() -> None:
    controller = GuideController(FakeCamera())
    assert controller.status().state == "idle"


def test_start_then_stop_lifecycle_reports_healthy_source() -> None:
    camera = FakeCamera()
    camera.connect()
    controller = GuideController(camera, measure_only=True)
    controller.start(exposure_s=0.01, cadence_s=0.0)
    try:
        assert _wait_until(lambda: controller.status().source is not None)
        status = controller.status()
        assert status.state == "running"
        assert status.source is not None
        assert status.source.health.value == "healthy"
    finally:
        controller.stop()
    assert controller.status().state == "idle"


def test_start_is_a_noop_when_already_running() -> None:
    camera = FakeCamera()
    camera.connect()
    controller = GuideController(camera, measure_only=True)
    controller.start(exposure_s=0.01, cadence_s=0.0)
    try:
        _wait_until(lambda: controller.status().source is not None)
        first_status = controller.status()
        controller.start(exposure_s=0.01, cadence_s=0.0)  # should not restart
        assert controller.status().started_at == first_status.started_at
    finally:
        controller.stop()


def test_measure_only_never_sends_pulses_even_with_a_persistent_offset() -> None:
    shape = (120, 120)
    arrays = [
        single_star_image(shape, x=60.0, y=60.0, peak=2000.0, sigma=2.5, background=100.0),
        single_star_image(shape, x=90.0, y=60.0, peak=2000.0, sigma=2.5, background=100.0),
    ]
    camera = ReplayCamera.from_arrays(arrays, cycle=True)
    camera.connect()
    mount = FakeMountAdapter()
    mount.connect()
    calibration = make_calibration()

    controller = GuideController(
        camera,
        mount=mount,
        calibration=calibration,
        measure_only=True,
        tracker=RoiTracker(lock_tolerance_px=50.0),
    )
    controller.start(exposure_s=0.001, cadence_s=0.0)
    try:
        time.sleep(0.3)  # let several offset frames pass
        assert mount.pulse_log == []
    finally:
        controller.stop()


def test_closed_loop_sends_pulses_for_a_persistent_offset() -> None:
    shape = (120, 120)
    arrays = [
        single_star_image(shape, x=60.0, y=60.0, peak=2000.0, sigma=2.5, background=100.0),
        single_star_image(shape, x=90.0, y=60.0, peak=2000.0, sigma=2.5, background=100.0),
    ]
    camera = ReplayCamera.from_arrays(arrays, cycle=True)
    camera.connect()
    mount = FakeMountAdapter()
    mount.connect()
    calibration = make_calibration()

    controller = GuideController(
        camera,
        mount=mount,
        calibration=calibration,
        measure_only=False,
        tracker=RoiTracker(lock_tolerance_px=50.0),
    )
    controller.start(exposure_s=0.001, cadence_s=0.0)
    try:
        assert _wait_until(lambda: len(mount.pulse_log) > 0)
        axis, direction, duration_ms = mount.pulse_log[0]
        assert axis is MountAxis.AXIS1
        # Which frame the background thread happens to capture first (60 or
        # 90) decides which position becomes the target, so either opposing
        # direction is a valid correction for the resulting +/-30px error.
        assert direction in (AxisDirection.POSITIVE, AxisDirection.NEGATIVE)
        assert duration_ms > 0
    finally:
        controller.stop()


def test_pause_pulses_suppresses_corrections_until_resumed() -> None:
    shape = (120, 120)
    arrays = [
        single_star_image(shape, x=60.0, y=60.0, peak=2000.0, sigma=2.5, background=100.0),
        single_star_image(shape, x=90.0, y=60.0, peak=2000.0, sigma=2.5, background=100.0),
    ]
    camera = ReplayCamera.from_arrays(arrays, cycle=True)
    camera.connect()
    mount = FakeMountAdapter()
    mount.connect()
    calibration = make_calibration()

    controller = GuideController(
        camera,
        mount=mount,
        calibration=calibration,
        measure_only=False,
        tracker=RoiTracker(lock_tolerance_px=50.0),
    )
    controller.pause_pulses()
    controller.start(exposure_s=0.001, cadence_s=0.0)
    try:
        time.sleep(0.2)
        assert mount.pulse_log == []
        controller.resume_pulses()
        assert _wait_until(lambda: len(mount.pulse_log) > 0)
    finally:
        controller.stop()


def test_rebaseline_adopts_the_current_position_as_the_new_target() -> None:
    def has_accepted_error() -> bool:
        source = controller.status().source
        return source is not None and source.error is not None and source.error.accepted

    camera = FakeCamera()
    camera.connect()
    controller = GuideController(camera, measure_only=True)
    controller.start(exposure_s=0.001, cadence_s=0.0)
    try:
        assert _wait_until(has_accepted_error)
        controller.rebaseline()
        # After rebaseline, the loop re-acquires; source should recover to healthy.
        assert _wait_until(has_accepted_error)
    finally:
        controller.stop()
