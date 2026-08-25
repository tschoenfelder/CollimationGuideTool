"""Shared CameraPort contract — every camera adapter must satisfy this.

Stage 2 factories: fake_camera_factory, fake_touptek_factory (both
hardware-free). replay_camera_factory / touptek_camera_factory join this
parametrization in Stage 3 once those adapters exist.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from astrotool_core.camera import CameraPort
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.testing.fake_touptek import FakeTouptekCamera

CameraFactory = Callable[[], CameraPort]


def fake_camera_factory() -> CameraPort:
    return FakeCamera()


def fake_touptek_factory() -> CameraPort:
    return FakeTouptekCamera()


CAMERA_FACTORIES = [fake_camera_factory, fake_touptek_factory]


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


@pytest.mark.parametrize("camera_factory", CAMERA_FACTORIES)
def test_connect_failure_raises_connection_error(camera_factory: CameraFactory) -> None:
    if camera_factory is fake_camera_factory:
        camera: CameraPort = FakeCamera(fail_connect=True)
    else:
        camera = FakeTouptekCamera(fail_connect=True)
    with pytest.raises(ConnectionError):
        camera.connect()
