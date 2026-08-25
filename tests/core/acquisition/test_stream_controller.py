import time

from astrotool_core.acquisition.acquisition_state import AcquisitionState
from astrotool_core.acquisition.stream_controller import StreamController
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.testing.fake_touptek import FakeTouptekCamera


def test_idle_before_streaming() -> None:
    controller = StreamController(FakeCamera())
    assert controller.state is AcquisitionState.IDLE


def test_start_stream_delivers_frames_to_the_mailbox() -> None:
    camera = FakeCamera()
    camera.connect()
    controller = StreamController(camera, name="test")
    controller.start_stream(exposure_s=0.01, cadence_s=0.0)
    try:
        streaming_state = controller.state
        assert streaming_state is AcquisitionState.STREAMING
        mailbox_frame = controller.mailbox.wait_latest(timeout_s=2.0)
        assert mailbox_frame is not None
        assert mailbox_frame.sequence >= 1
        assert mailbox_frame.frame.height > 0
    finally:
        controller.stop_stream()
    idle_state = controller.state
    assert idle_state is AcquisitionState.IDLE


def test_stop_stream_is_idempotent_and_safe_when_never_started() -> None:
    controller = StreamController(FakeCamera())
    controller.stop_stream()  # must not raise


def test_start_stream_twice_is_a_noop() -> None:
    camera = FakeCamera()
    camera.connect()
    controller = StreamController(camera, name="dup")
    controller.start_stream(exposure_s=0.01, cadence_s=0.01)
    first_thread = controller._thread
    controller.start_stream(exposure_s=0.01, cadence_s=0.01)
    assert controller._thread is first_thread
    controller.stop_stream()


def test_capture_error_surfaces_via_pop_stream_error_and_stops_streaming() -> None:
    camera = FakeTouptekCamera(fail_on_capture=1)
    controller = StreamController(camera, name="erroring")
    controller.start_stream(exposure_s=0.01, cadence_s=0.0)
    deadline = time.monotonic() + 2.0
    while controller.state is AcquisitionState.STREAMING and time.monotonic() < deadline:
        time.sleep(0.02)
    assert controller.state is AcquisitionState.ERROR
    error = controller.pop_stream_error()
    assert error is not None
    assert controller.pop_stream_error() is None  # consumed
    controller.stop_stream()


def test_mailbox_drops_intermediate_frames_and_counts_them() -> None:
    from astrotool_core.acquisition.stream_controller import FrameMailbox
    from astrotool_core.testing.frame_factory import make_frame, single_star_image

    mailbox = FrameMailbox()
    frame_a = make_frame(single_star_image((8, 8), x=4, y=4, peak=100.0))
    frame_b = make_frame(single_star_image((8, 8), x=4, y=4, peak=200.0))

    mailbox.put(frame_a, sequence=1, captured_at=0.0)
    mailbox.put(frame_b, sequence=2, captured_at=0.1)  # frame_a dropped

    latest = mailbox.wait_latest(timeout_s=0.1)
    assert latest is not None
    assert latest.sequence == 2
    assert latest.dropped_before == 1
    assert mailbox.dropped_count == 1


def test_wait_latest_times_out_when_nothing_new() -> None:
    from astrotool_core.acquisition.stream_controller import FrameMailbox

    mailbox = FrameMailbox()
    assert mailbox.wait_latest(timeout_s=0.05) is None
