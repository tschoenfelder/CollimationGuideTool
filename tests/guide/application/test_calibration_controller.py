import numpy as np
import pytest
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.frame_factory import single_star_image
from guide_tool.application.calibration_controller import run_calibration


def test_run_calibration_builds_a_full_matrix_from_a_moving_replay_sequence() -> None:
    shape = (240, 240)
    # acquire, then before/after pairs for AXIS1 POSITIVE/NEGATIVE and
    # AXIS2 POSITIVE/NEGATIVE (calibrate_axes calls measure() 8 times for
    # 2 axes x 2 directions, each with a before+after capture).
    positions = [
        (100.0, 100.0),  # AXIS1 POSITIVE before
        (120.0, 100.0),  # AXIS1 POSITIVE after (+20 x)
        (100.0, 100.0),  # AXIS1 NEGATIVE before
        (80.0, 100.0),  # AXIS1 NEGATIVE after (-20 x)
        (100.0, 100.0),  # AXIS2 POSITIVE before
        (100.0, 115.0),  # AXIS2 POSITIVE after (+15 y)
        (100.0, 100.0),  # AXIS2 NEGATIVE before
        (100.0, 85.0),  # AXIS2 NEGATIVE after (-15 y)
    ]
    arrays = [
        single_star_image(shape, x=x, y=y, peak=2000.0, sigma=2.5, background=100.0)
        for x, y in positions
    ]
    camera = ReplayCamera.from_arrays(arrays, cycle=False)
    camera.connect()
    mount = FakeMountAdapter()
    mount.connect()

    matrix = run_calibration(camera, mount, pulse_ms=500, lock_tolerance_px=30.0)

    axis1_pos = matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
    assert axis1_pos.dx_px == pytest.approx(20.0, abs=0.5)
    axis2_neg = matrix.response_for(MountAxis.AXIS2, AxisDirection.NEGATIVE)
    assert axis2_neg.dy_px == pytest.approx(-15.0, abs=0.5)


def test_run_calibration_raises_when_no_star_found() -> None:
    dark = np.full((64, 64), 100.0, dtype=np.float32)
    camera = ReplayCamera.from_arrays([dark], cycle=False)
    camera.connect()
    mount = FakeMountAdapter()
    mount.connect()

    with pytest.raises(RuntimeError, match="no guide star found"):
        run_calibration(camera, mount)
