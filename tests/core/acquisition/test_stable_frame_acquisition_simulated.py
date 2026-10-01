"""Issue #51 (for #30/#43/#49): stale and motion-overlapping frames generated
deterministically with explicit timestamps (`FrameTimeline` on a FakeClock),
fed to the production `acquire_stable_frame` / `acquire_verified_frame`."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astrotool_core.acquisition.stable_frame_acquisition import FrameAcquisitionStatus
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
from astrotool_core.testing import (
    CameraFrameSource,
    FrameSpec,
    FrameTimeline,
    SimulatedToupcamSdk,
    frames_from_replay,
    install_simulated_toupcam,
    load_frames,
)
from astrotool_core.timing import FakeClock


def _texture(dx: int = 0) -> np.ndarray:
    rng = np.random.default_rng(7)
    base = rng.random((48, 48)).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(3) for j in range(3))
    return np.roll(np.asarray(smooth * 100.0 + 50.0, dtype=np.float32), dx, axis=1)


class TestExposureOverlappingMotionIsNeverUsed:
    """Diagnostic c7dc2c3d ("still using frames during movement"), fixed by
    c4847ab: a frame DELIVERED after the motion ended can still have STARTED
    its exposure during the motion. With a 2 s exposure and the move ending
    at t=10.0, the frame delivered at 10.5 (exposure 8.5-10.5) integrated
    motion; the first trustworthy frame is the one delivered at 12.5."""

    def test_a_frame_delivered_after_the_move_but_exposed_during_it_is_skipped(self) -> None:
        clock = FakeClock(start=10.0)  # the move has just ended
        timeline = (
            FrameTimeline(clock)
            .add_motion(7.0, 10.0)
            .schedule_stream(first_start=8.5, exposure_s=2.0, cadence_s=2.0, count=3)
        )
        result = timeline.waiter()(10.0, 5.0)

        assert result.status is FrameAcquisitionStatus.OK
        assert result.frame is not None
        assert result.frame.captured_at_monotonic == 12.5
        assert result.frame.captured_at_monotonic - result.frame.exposure_seconds == 10.5
        assert [f.overlaps_motion for f in timeline.delivered] == [True, False]
        assert clock.monotonic() == 12.5

    def test_only_overlapping_frames_before_the_deadline_is_its_own_status(self) -> None:
        clock = FakeClock(start=10.0)
        timeline = FrameTimeline(clock).add(FrameSpec(8.5, 2.0), FrameSpec(14.0, 2.0))
        result = timeline.waiter()(10.0, 3.0)
        assert result.status is FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION
        assert clock.monotonic() == pytest.approx(13.0)


class TestStaleQueuedFrames:
    def test_a_backlog_of_frames_from_before_the_move_is_drained_not_used(self) -> None:
        """#49's ring holds recent frames: everything already queued when the
        wait starts predates the reference and must be skipped, in order."""
        clock = FakeClock(start=20.0)
        timeline = FrameTimeline(clock).schedule_stream(
            first_start=15.0, exposure_s=0.5, cadence_s=1.0, count=8
        )
        result = timeline.waiter()(20.0, 5.0)
        assert result.ok and result.frame is not None
        assert result.frame.captured_at_monotonic == 20.5
        assert [f.spec.captured_at for f in timeline.delivered] == [
            15.5,
            16.5,
            17.5,
            18.5,
            19.5,
            20.5,
        ]

    def test_nothing_new_in_time_is_a_timeout_on_fake_time(self) -> None:
        clock = FakeClock(start=0.0)
        result = FrameTimeline(clock).waiter()(0.0, 4.0)
        assert result.status is FrameAcquisitionStatus.TIMEOUT
        assert clock.monotonic() == 4.0


class TestReplayAndCameraSources:
    def test_captured_fits_frames_replay_with_explicit_timestamps(self, tmp_path: Path) -> None:
        for index in range(3):
            fits.PrimaryHDU(_texture(dx=index).astype(np.uint16)).writeto(
                tmp_path / f"frame_{index:03d}.fits"
            )
        clock = FakeClock()
        timeline = (
            FrameTimeline(clock)
            .use_pixels(frames_from_replay(load_frames(tmp_path)))
            .schedule_stream(first_start=0.0, exposure_s=1.0, cadence_s=1.0, count=3)
        )
        delivered = [timeline.next_frame(5.0) for _ in range(3)]
        assert [f.captured_at_monotonic for f in delivered if f is not None] == [1.0, 2.0, 3.0]
        assert all(f is not None for f in delivered)
        np.testing.assert_array_equal(
            delivered[2].pixels if delivered[2] is not None else None,
            _texture(dx=2).astype(np.uint16).astype(np.float32),
        )

    def test_a_simulated_camera_feeds_the_same_freshness_rule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = FakeClock(start=100.0)
        sdk = SimulatedToupcamSdk(clock=clock)
        device = sdk.add_camera("G3M678M")
        install_simulated_toupcam(monkeypatch, sdk)
        camera = TouptekCameraAdapter(camera_id=device.id)
        camera.connect()
        reference = clock.monotonic()
        result = CameraFrameSource(camera, clock, exposure_s=2.0).waiter()(reference, 10.0)
        assert result.ok and result.frame is not None
        assert result.frame.captured_at_monotonic - result.frame.exposure_seconds == reference
