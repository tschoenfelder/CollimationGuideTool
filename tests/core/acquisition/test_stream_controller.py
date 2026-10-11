import time

import numpy as np
import pytest
from astrotool_core.acquisition.acquisition_state import AcquisitionState
from astrotool_core.acquisition.stream_controller import FrameMailbox, StreamController
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.frames.frame import Frame
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


class TestFrameMailboxRing:
    """Issue #49: a worker thread must be able to wait for fresh frames WITHOUT stealing them
    from the live-view consumer (the destructive `wait_latest`), or the calibration capture
    waits had to run on the GUI thread and froze the UI for tens of seconds."""

    @staticmethod
    def _frame(marker: int) -> Frame:
        return Frame(pixels=np.full((2, 2), float(marker)), header={}, exposure_seconds=0.1)

    def _mailbox(self) -> FrameMailbox:
        return FrameMailbox()

    def test_wait_next_after_returns_the_oldest_newer_frame_in_order(self) -> None:
        mailbox = self._mailbox()
        for seq in (1, 2, 3):
            mailbox.put(self._frame(seq), sequence=seq, captured_at=float(seq))

        first = mailbox.wait_next_after(0, timeout_s=0.1)
        second = mailbox.wait_next_after(first.sequence, timeout_s=0.1)  # type: ignore[union-attr]
        third = mailbox.wait_next_after(second.sequence, timeout_s=0.1)  # type: ignore[union-attr]

        assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]  # type: ignore[union-attr]

    def test_reading_is_non_destructive_for_the_live_view_consumer(self) -> None:
        mailbox = self._mailbox()
        mailbox.put(self._frame(1), sequence=1, captured_at=1.0)

        assert mailbox.wait_next_after(0, timeout_s=0.1) is not None
        popped = mailbox.wait_latest(after_sequence=0, timeout_s=0.1)  # the display consumer

        assert popped is not None and popped.sequence == 1  # the worker did not steal it

    def test_the_destructive_consumer_does_not_starve_the_worker(self) -> None:
        mailbox = self._mailbox()
        mailbox.put(self._frame(1), sequence=1, captured_at=1.0)
        assert mailbox.wait_latest(after_sequence=0, timeout_s=0.1) is not None  # display pops it

        seen = mailbox.wait_next_after(0, timeout_s=0.1)

        assert seen is not None and seen.sequence == 1  # still visible to the worker (ring)

    def test_it_blocks_until_a_newer_frame_arrives_then_wakes(self) -> None:
        import threading

        mailbox = self._mailbox()
        mailbox.put(self._frame(1), sequence=1, captured_at=1.0)
        threading.Timer(
            0.1, lambda: mailbox.put(self._frame(2), sequence=2, captured_at=2.0)
        ).start()

        started = time.monotonic()
        result = mailbox.wait_next_after(1, timeout_s=2.0)

        assert result is not None and result.sequence == 2
        assert 0.05 < time.monotonic() - started < 1.5

    def test_it_times_out_with_none_when_nothing_newer_arrives(self) -> None:
        mailbox = self._mailbox()
        mailbox.put(self._frame(1), sequence=1, captured_at=1.0)

        started = time.monotonic()
        assert mailbox.wait_next_after(1, timeout_s=0.15) is None
        assert time.monotonic() - started >= 0.14

    def test_the_ring_is_bounded(self) -> None:
        mailbox = self._mailbox()
        for seq in range(1, 30):
            mailbox.put(self._frame(seq), sequence=seq, captured_at=float(seq))

        oldest = mailbox.wait_next_after(0, timeout_s=0.1)

        assert oldest is not None and oldest.sequence >= 30 - 8  # only the last few are kept

    def test_wait_latest_behaviour_is_unchanged(self) -> None:
        mailbox = self._mailbox()
        for seq in (1, 2, 3):
            mailbox.put(self._frame(seq), sequence=seq, captured_at=float(seq))

        latest = mailbox.wait_latest(after_sequence=0, timeout_s=0.1)

        assert latest is not None and latest.sequence == 3
        assert mailbox.wait_latest(after_sequence=0, timeout_s=0.05) is None  # popped
        assert mailbox.dropped_count == 2


class _StampedCamera(FakeCamera):
    """S6.7: a camera whose frames say when their exposure started (like the ToupTek
    adapter's trigger stamp), 5 s before capture() returns (long readout/pull)."""

    def capture(self, exposure_seconds: float) -> Frame:
        frame = super().capture(exposure_seconds)
        return Frame(
            pixels=frame.pixels,
            header=frame.header,
            exposure_seconds=2.0,
            exposure_started_at=time.monotonic() - 5.0,
        )


def test_a_known_exposure_start_stamps_the_end_of_the_exposure_not_of_the_readout() -> None:
    """S6.7: consumers derive the exposure start as captured_at - exposure_seconds; the
    after-capture stamp put it AFTER the real start. With a known start the mailbox stamp is
    start + exposure (here 3 s before capture() returned)."""
    camera = _StampedCamera()
    camera.connect()
    controller = StreamController(camera, name="stamped")
    controller.start_stream(exposure_s=0.01, cadence_s=0.05)
    try:
        mailbox_frame = controller.mailbox.wait_latest(timeout_s=2.0)
    finally:
        controller.stop_stream()
    assert mailbox_frame is not None
    started = mailbox_frame.frame.exposure_started_at
    assert started is not None
    assert mailbox_frame.captured_at_monotonic - 2.0 == pytest.approx(started, abs=1e-6)
    assert mailbox_frame.captured_at_monotonic < time.monotonic() - 2.5


def test_without_a_known_start_the_after_capture_stamp_is_kept() -> None:
    camera = FakeCamera()
    camera.connect()
    controller = StreamController(camera, name="plain")
    before = time.monotonic()
    controller.start_stream(exposure_s=0.01, cadence_s=0.05)
    try:
        mailbox_frame = controller.mailbox.wait_latest(timeout_s=2.0)
    finally:
        controller.stop_stream()
    assert mailbox_frame is not None
    assert mailbox_frame.frame.exposure_started_at is None
    assert mailbox_frame.captured_at_monotonic >= before


def test_the_capture_cadence_waits_on_the_injected_clock_and_stop_ends_it() -> None:
    """S6.3 (#53, S3b finding (d)): the cadence wait goes through the injected `Clock` with
    the stop event as its cancel -- driven on fake time, and Stop wakes it at once."""
    from astrotool_core.timing import FakeClock

    clock = FakeClock(start=50.0, auto_advance=False)
    camera = FakeCamera()
    camera.connect()
    controller = StreamController(camera, name="clocked", clock=clock)
    controller.start_stream(exposure_s=0.01, cadence_s=1.0)
    try:
        assert clock.wait_for_sleepers(1)
        assert clock.sleeps == [1.0]  # no fake time passed during the capture
        first = controller.mailbox.wait_latest(timeout_s=1.0)
        assert first is not None and first.sequence == 1
        assert first.captured_at_monotonic <= 50.0
        clock.advance(1.0)  # the cadence elapses -> the next capture
        second = controller.mailbox.wait_latest(after_sequence=1, timeout_s=2.0)
        assert second is not None and second.sequence == 2
        assert clock.wait_for_sleepers(1)
    finally:
        started = time.monotonic()
        controller.stop_stream()
    assert time.monotonic() - started < 2.0  # the wait was cancelled, not slept out
    assert controller.state is AcquisitionState.IDLE
