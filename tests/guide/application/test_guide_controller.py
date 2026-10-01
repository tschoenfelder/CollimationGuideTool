from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest
from astrotool_core.acquisition.stream_controller import MailboxFrame
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.frames.frame import Frame
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.target.roi_tracker import RoiTracker
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.frame_factory import single_star_image
from astrotool_core.timing import FakeClock
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
    """Only for the few threaded lifecycle smoke tests below; returns as soon
    as the predicate holds. Never used to wait for a guiding *result*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


# --- Threaded lifecycle smoke tests (real stream + guiding threads) ----------


def test_idle_before_start() -> None:
    controller = GuideController(FakeCamera())
    assert controller.status().state == "idle"


def test_start_then_stop_lifecycle_reports_healthy_source() -> None:
    camera = FakeCamera()
    camera.connect()
    controller = GuideController(camera, measure_only=True)
    # A small positive cadence (not 0.0, an unthrottled tight loop) — this
    # test only needs a handful of captures to observe the running/healthy
    # state, not maximum throughput. Found investigating an intermittent
    # (roughly 1-in-8 on a real Pi, reliably reproducing on GitHub's
    # x86_64 CI runner) segfault deep inside smarttscope_live_analysis's
    # np.count_nonzero, in this controller's background analysis thread,
    # racing StreamController's own capture thread. This file/dependency
    # weren't touched by whatever change first exposed it, and the crash
    # site is native code this project doesn't own — a real, apparently
    # pre-existing race under this tight a loop, not something proven
    # fixed here, just made less likely to trigger.
    controller.start(exposure_s=0.01, cadence_s=0.02)
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


def test_start_reads_its_start_time_from_the_injected_clock() -> None:
    camera = FakeCamera()
    camera.connect()
    controller = GuideController(camera, measure_only=True, clock=FakeClock(42.0))
    controller.start(exposure_s=0.01, cadence_s=0.02)
    try:
        assert controller.status().started_at == 42.0
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


# --- Deterministic per-frame tests (issues #13, #53) -------------------------
#
# The two #13 flakes (`test_closed_loop_sends_pulses_for_a_persistent_offset`,
# `test_pause_pulses_suppresses_corrections_until_resumed`) used to start the
# real stream and guiding threads on a ReplayCamera alternating between x=60
# and x=90, then wait up to 2 s of wall clock for "some pulse". Whether a pulse
# came depended on which frames the single-slot mailbox happened to drop. They
# now feed exactly the frames the scenario is about through
# `process_mailbox_frame` (the guiding loop's own per-frame step) on a
# FakeClock: frame 1 at x=60 acquires the target, every later frame at x=90 is
# a persistent +30 px error. No threads, no real waits.

_SHAPE = (120, 120)
_MAX_FRAME_AGE_S = 2.0


def _star_frame(x: float) -> Frame:
    pixels = single_star_image(_SHAPE, x=x, y=60.0, peak=2000.0, sigma=2.5, background=100.0)
    return Frame(pixels=pixels, header=None, exposure_seconds=0.001)


def _mailbox(sequence: int, frame: Frame, captured_at: float) -> MailboxFrame:
    return MailboxFrame(sequence=sequence, captured_at_monotonic=captured_at, frame=frame)


def _offset_controller(
    *, measure_only: bool, clock: FakeClock, fallback_after_bad_frames: int = 5
) -> tuple[GuideController, FakeMountAdapter]:
    mount = FakeMountAdapter()
    mount.connect()
    controller = GuideController(
        FakeCamera(),
        mount=mount,
        calibration=make_calibration(),
        measure_only=measure_only,
        tracker=RoiTracker(lock_tolerance_px=50.0),
        max_frame_age_s=_MAX_FRAME_AGE_S,
        fallback_after_bad_frames=fallback_after_bad_frames,
        clock=clock,
    )
    return controller, mount


def _feed_offset_frames(controller: GuideController, clock: FakeClock, count: int) -> None:
    """Frame 1 acquires the target at x=60; frames 2..count sit at x=90."""
    for sequence in range(1, count + 1):
        frame = _star_frame(60.0 if sequence == 1 else 90.0)
        controller.process_mailbox_frame(_mailbox(sequence, frame, clock.monotonic()))
        clock.advance(0.5)


def test_measure_only_never_sends_pulses_even_with_a_persistent_offset() -> None:
    clock = FakeClock(100.0)
    controller, mount = _offset_controller(measure_only=True, clock=clock)

    _feed_offset_frames(controller, clock, count=6)

    assert mount.pulse_log == []
    assert controller.status().latest_pulses  # computed, just never sent


def test_closed_loop_sends_pulses_for_a_persistent_offset() -> None:
    clock = FakeClock(100.0)
    controller, mount = _offset_controller(measure_only=False, clock=clock)

    _feed_offset_frames(controller, clock, count=2)

    assert len(mount.pulse_log) == 1
    axis, direction, duration_ms = mount.pulse_log[0]
    assert axis is MountAxis.AXIS1
    # The star sits +30 px in x from its target, and AXIS1 POSITIVE moves it
    # +10 px per 100 ms: the correction must push it back, AXIS1 NEGATIVE.
    assert direction is AxisDirection.NEGATIVE
    assert duration_ms > 0


def test_pause_pulses_suppresses_corrections_until_resumed() -> None:
    clock = FakeClock(100.0)
    controller, mount = _offset_controller(measure_only=False, clock=clock)
    controller.pause_pulses()

    _feed_offset_frames(controller, clock, count=4)
    assert mount.pulse_log == []

    controller.resume_pulses()
    controller.process_mailbox_frame(_mailbox(5, _star_frame(90.0), clock.monotonic()))

    assert len(mount.pulse_log) == 1


class TestFrameAgeBoundary:
    """`max_frame_age_s` is inclusive: a frame exactly that old still counts
    as good, anything older is a bad frame (fake time, exact ages)."""

    def _bad_count_after_second_frame(self, age_s: float) -> int:
        clock = FakeClock(1000.0)
        controller, _mount = _offset_controller(measure_only=True, clock=clock)
        controller.process_mailbox_frame(_mailbox(1, _star_frame(60.0), clock.monotonic()))
        captured_at = clock.monotonic()
        clock.advance(age_s)
        status = controller.process_mailbox_frame(_mailbox(2, _star_frame(90.0), captured_at))
        assert status.source is not None
        assert status.source.latest_frame_age_s == pytest.approx(age_s)
        return status.source.bad_frame_count

    def test_just_younger_than_the_limit_is_good(self) -> None:
        assert self._bad_count_after_second_frame(_MAX_FRAME_AGE_S - 0.001) == 0

    def test_exactly_at_the_limit_is_still_good(self) -> None:
        assert self._bad_count_after_second_frame(_MAX_FRAME_AGE_S) == 0

    def test_just_older_than_the_limit_is_bad(self) -> None:
        assert self._bad_count_after_second_frame(_MAX_FRAME_AGE_S + 0.001) == 1


class TestBadFrameFallback:
    def test_health_degrades_exactly_at_the_bad_frame_threshold(self) -> None:
        controller, _mount = _offset_controller(
            measure_only=True, clock=FakeClock(), fallback_after_bad_frames=3
        )
        healths = []
        for _ in range(3):
            status = controller.process_mailbox_frame(None)  # nothing arrived in time
            assert status.source is not None
            healths.append(status.source.health.value)
        assert healths == ["healthy", "healthy", "transient_bad"]

    def test_a_good_frame_resets_the_bad_count(self) -> None:
        clock = FakeClock()
        controller, _mount = _offset_controller(
            measure_only=True, clock=clock, fallback_after_bad_frames=3
        )
        controller.process_mailbox_frame(None)
        controller.process_mailbox_frame(None)
        status = controller.process_mailbox_frame(
            _mailbox(1, _star_frame(60.0), clock.monotonic())
        )
        assert status.source is not None
        assert status.source.bad_frame_count == 0
        assert status.source.health.value == "healthy"

    def test_a_stream_error_is_a_hard_failure(self) -> None:
        controller, _mount = _offset_controller(measure_only=True, clock=FakeClock())
        status = controller.process_mailbox_frame(None, stream_error=RuntimeError("usb gone"))
        assert status.source is not None
        assert status.source.health.value == "hard_failed"
        assert status.source.hard_failure == "usb gone"


# --- start()/stop() lifecycle with a fake stream (no camera, no real waits) --
#
# `StreamController` is replaced by a fake whose mailbox blocks the guiding
# loop until stop(): while the worker is parked there, the test thread drives
# `process_mailbox_frame` itself, so nothing races.


class _ParkedMailbox:
    def __init__(self, released: threading.Event) -> None:
        self._released = released
        self.after_sequences: list[int] = []
        self.loop_waiting = threading.Event()

    def wait_latest(self, *, after_sequence: int, timeout_s: float) -> MailboxFrame | None:
        self.after_sequences.append(after_sequence)
        self.loop_waiting.set()
        self._released.wait(timeout=5.0)  # released by stop(); bounded safety net only
        return None


class _FakeStream:
    instances: list[_FakeStream] = []

    def __init__(self, _camera: object, *, name: str = "camera") -> None:
        self._released = threading.Event()
        self.mailbox = _ParkedMailbox(self._released)
        self.started = False
        _FakeStream.instances.append(self)

    def start_stream(self, exposure_s: float, cadence_s: float) -> None:
        self.started = True

    def stop_stream(self) -> None:
        self._released.set()

    def pop_stream_error(self) -> Exception | None:
        return None


@pytest.fixture
def fake_streams(monkeypatch: pytest.MonkeyPatch) -> list[_FakeStream]:
    import guide_tool.application.guide_controller as module

    _FakeStream.instances = []
    monkeypatch.setattr(module, "StreamController", _FakeStream)
    return _FakeStream.instances


def _start_and_park(controller: GuideController, streams: list[_FakeStream]) -> _FakeStream:
    controller.start(exposure_s=0.01, cadence_s=0.0)
    stream = streams[-1]
    assert stream.mailbox.loop_waiting.wait(timeout=5.0)  # barrier: loop is parked
    return stream


def test_a_pause_set_before_start_survives_start(fake_streams: list[_FakeStream]) -> None:
    """Regression guard for 9087272 (kept by #13): start() must not clobber a
    pause_pulses() issued before it."""
    clock = FakeClock(100.0)
    controller, mount = _offset_controller(measure_only=False, clock=clock)
    controller.pause_pulses()
    _start_and_park(controller, fake_streams)
    try:
        _feed_offset_frames(controller, clock, count=3)
        assert mount.pulse_log == []  # still paused after start()
        controller.resume_pulses()
        controller.process_mailbox_frame(_mailbox(4, _star_frame(90.0), clock.monotonic()))
        assert len(mount.pulse_log) == 1  # and the scenario does produce pulses
    finally:
        controller.stop()


def test_a_step_on_an_idle_controller_neither_claims_a_run_nor_blocks_start(
    fake_streams: list[_FakeStream],
) -> None:
    clock = FakeClock()
    controller, _mount = _offset_controller(measure_only=True, clock=clock)

    status = controller.process_mailbox_frame(_mailbox(1, _star_frame(60.0), clock.monotonic()))
    assert status.state == "idle"
    assert controller.status().state == "idle"

    stream = _start_and_park(controller, fake_streams)
    try:
        assert stream.started
        assert controller.status().state == "running"
    finally:
        controller.stop()
    assert controller.status().state == "idle"


def test_restart_begins_with_fresh_loop_bookkeeping(fake_streams: list[_FakeStream]) -> None:
    clock = FakeClock()
    controller, _mount = _offset_controller(
        measure_only=True, clock=clock, fallback_after_bad_frames=10
    )
    _start_and_park(controller, fake_streams)
    try:
        controller.process_mailbox_frame(_mailbox(7, _star_frame(60.0), clock.monotonic()))
        controller.process_mailbox_frame(None)
        status = controller.process_mailbox_frame(None)
        assert status.source is not None
        assert (status.source.latest_sequence, status.source.bad_frame_count) == (7, 2)
        assert status.latest_pixels is not None
    finally:
        controller.stop()

    second = _start_and_park(controller, fake_streams)
    try:
        assert second.mailbox.after_sequences[0] == 0  # loop asks from the start again
        status = controller.process_mailbox_frame(None)
        assert status.source is not None
        assert status.source.latest_sequence == 0
        assert status.source.bad_frame_count == 1
        assert status.latest_pixels is None
    finally:
        controller.stop()
