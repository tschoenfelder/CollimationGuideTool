from astrotool_core.acquisition.acquisition_state import AcquisitionState
from astrotool_core.acquisition.single_capture import capture_once
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.testing.fake_touptek import FakeTouptekCamera


def test_successful_capture_returns_idle_with_a_frame() -> None:
    camera = FakeCamera()
    camera.connect()
    result = capture_once(camera, 0.5)
    assert result.state is AcquisitionState.IDLE
    assert result.frame is not None
    assert result.error is None


def test_capture_error_is_reported_as_error_state() -> None:
    camera = FakeTouptekCamera(fail_on_capture=1)
    result = capture_once(camera, 0.5)
    assert result.state is AcquisitionState.ERROR
    assert result.frame is None
    assert result.error is not None


def test_capture_abort_is_reported_as_aborted_state() -> None:
    camera = FakeTouptekCamera(capture_delay_s=10.0)
    camera.abort_capture()  # pre-set so the wait() returns immediately as "signaled"
    result = capture_once(camera, 0.1)
    assert result.state is AcquisitionState.ABORTED
    assert result.frame is None
