"""Issue #51 (for #30/#43): `acquire_verified_frame` over a simulated frame
stream with explicit timestamps (`FrameTimeline` on a FakeClock): frames from
a still-drifting scene are not verified; the verified frame comes from the
still window."""

from __future__ import annotations

import numpy as np
from astrotool_core.acquisition.motion_aware_acquisition import (
    MotionAwareStatus,
    acquire_verified_frame,
)
from astrotool_core.testing import FrameTimeline
from astrotool_core.timing import FakeClock


def _texture(dx: int = 0) -> np.ndarray:
    rng = np.random.default_rng(7)
    base = rng.random((48, 48)).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(3) for j in range(3))
    return np.roll(np.asarray(smooth * 100.0 + 50.0, dtype=np.float32), dx, axis=1)


class TestMotionAwareAcquisition:
    def test_frames_from_a_still_moving_scene_are_unstable_then_settle(self) -> None:
        """#30/#43: the image keeps drifting 3 px per frame until t=13, then
        holds. The verified frame must come from the still window."""
        clock = FakeClock(start=10.0)

        def render(start: float, _end: float) -> np.ndarray:
            return _texture(dx=3 * int(min(start, 13.0) - 10.0))

        timeline = FrameTimeline(clock, render=render).schedule_stream(
            first_start=10.0, exposure_s=0.5, cadence_s=1.0, count=10
        )
        result = acquire_verified_frame(
            timeline.waiter(),
            reference_monotonic=10.0,
            timeout_s=10.0,
            stability_tolerance_px=0.5,
            stability_sample_count=3,
            stability_sample_interval_s=0.0,
            clock=clock,
        )
        assert result.status is MotionAwareStatus.OK
        starts = [t["exposure_start_monotonic"] for t in result.diagnostics["frame_timings"]]
        assert min(starts) >= 13.0
