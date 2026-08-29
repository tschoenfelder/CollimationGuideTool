"""Shared CameraPort contract — every camera adapter must satisfy this.

fake_camera_factory / fake_touptek_factory / replay_camera_factory run
hardware-free. touptek_camera_factory is real-hardware and skipif-guarded —
no toupcam SDK/camera is present in this Windows dev environment.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
from astrotool_core.camera import CameraPort
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
from astrotool_core.testing.fake_touptek import FakeTouptekCamera

CameraFactory = Callable[[], CameraPort]


def fake_camera_factory() -> CameraPort:
    return FakeCamera()


def fake_touptek_factory() -> CameraPort:
    return FakeTouptekCamera()


def replay_camera_factory() -> CameraPort:
    return ReplayCamera.from_arrays([np.full((64, 64), 500.0, dtype=np.float32)])


def touptek_camera_factory() -> CameraPort:
    return TouptekCameraAdapter()


def _toupcam_sdk_available() -> bool:
    try:
        import toupcam  # noqa: F401
    except ImportError:
        return False
    return True


CAMERA_FACTORIES = [fake_camera_factory, fake_touptek_factory, replay_camera_factory]
REAL_CAMERA_FACTORIES = [
    pytest.param(
        touptek_camera_factory,
        marks=pytest.mark.skipif(
            not _toupcam_sdk_available(), reason="toupcam SDK not installed in this environment"
        ),
    ),
]


@pytest.mark.parametrize("camera_factory", CAMERA_FACTORIES)
def test_connect_then_capture_returns_a_frame_with_positive_dimensions(
    camera_factory: CameraFactory,
) -> None:
    camera = camera_factory()
    camera.connect()
    try:
        frame = camera.capture(0.5)
        assert frame.height > 0
        assert frame.width > 0
        assert frame.exposure_seconds == 0.5
    finally:
        camera.disconnect()


@pytest.mark.parametrize("camera_factory", CAMERA_FACTORIES)
def test_descriptor_reports_capabilities_and_identity(camera_factory: CameraFactory) -> None:
    camera = camera_factory()
    descriptor = camera.get_descriptor()
    assert descriptor.logical_name
    assert descriptor.capabilities.bit_depth > 0
    assert descriptor.capabilities.sensor_width_px > 0
    assert descriptor.capabilities.sensor_height_px > 0


@pytest.mark.parametrize("camera_factory", CAMERA_FACTORIES)
def test_exposure_gain_black_level_round_trip(camera_factory: CameraFactory) -> None:
    camera = camera_factory()
    camera.set_exposure_ms(500.0)
    assert camera.get_exposure_ms() == 500.0
    camera.set_gain(200)
    assert camera.get_gain() == 200
    camera.set_black_level(10)
    assert camera.get_black_level() == 10


@pytest.mark.parametrize("camera_factory", CAMERA_FACTORIES)
def test_abort_capture_is_safe_to_call_when_idle(camera_factory: CameraFactory) -> None:
    camera = camera_factory()
    camera.abort_capture()  # must not raise


@pytest.mark.parametrize("camera_factory", [fake_camera_factory, fake_touptek_factory])
def test_connect_failure_raises_connection_error(camera_factory: CameraFactory) -> None:
    if camera_factory is fake_camera_factory:
        camera: CameraPort = FakeCamera(fail_connect=True)
    else:
        camera = FakeTouptekCamera(fail_connect=True)
    with pytest.raises(ConnectionError):
        camera.connect()


@pytest.mark.parametrize("camera_factory", REAL_CAMERA_FACTORIES)
def test_real_touptek_connect_then_capture(camera_factory: CameraFactory) -> None:
    camera = camera_factory()
    camera.connect()
    try:
        frame = camera.capture(0.1)
        assert frame.height > 0
        assert frame.width > 0
    finally:
        camera.disconnect()


@pytest.mark.parametrize("camera_factory", REAL_CAMERA_FACTORIES)
def test_real_touptek_live_exposure_change_survives_the_next_capture(
    camera_factory: CameraFactory,
) -> None:
    """Regression test for issue #14, found via real-hardware testing:
    capture() used to unconditionally re-apply its own exposure_seconds
    argument to hardware on every call, silently reverting any
    set_exposure_ms() made in between — exactly what happens when a live
    UI adjusts exposure while streaming (manual spinbox edits and the
    auto-exposure feature both call set_exposure_ms() mid-stream), and
    StreamController always calls capture() with the exposure it was
    started with. Exposure appeared "stuck" no matter what the UI did.
    """
    camera = camera_factory()
    camera.connect()
    try:
        camera.capture(0.001)  # bootstrap capture at a tiny exposure
        camera.set_exposure_ms(50.0)  # a live adjustment, as the UI makes
        # StreamController would keep passing the *original* 0.001s here.
        frame = camera.capture(0.001)
        assert camera.get_exposure_ms() == pytest.approx(50.0, rel=0.2)
        assert frame.exposure_seconds == pytest.approx(0.05, rel=0.2)
    finally:
        camera.disconnect()
