"""S6.7 (#52, S3b(c); #43/#30 class): Mount Align's capture timing follows the shared
fresh-frame-after-motion rule on the INJECTED clock -- the motion reference
(`measurement_reference(completed_at, frame_settle)`), the exposure-scaled budget
(`DEFAULT_FRESH_FRAME_BUDGET`), the stability window's waits and the verified reference's
60 s max age -- so it runs deterministically on fake time.

Frames come from `FrameTimeline` streams (#51) with explicit exposure start/length and a stale
backlog on the same FakeClock; the production `acquire_stable_frame` judges every frame and
the production `acquire_verified_frames` runs the stability window."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.acquisition import DEFAULT_FRESH_FRAME_BUDGET
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, MountAxis
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.sim_frames import FrameSpec, FrameTimeline
from astrotool_core.timing import FakeClock
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel
from collimation_tool.ui.mount_test_move_runner import MountPulseOutcome, MountTestMoveRunner

_SHAPE = (100, 140)
_FRAME_SETTLE_MS = 500


def _texture(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random(_SHAPE).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0, dtype=np.float32)


class _Rig:
    def __init__(self, *, exposure_s: float, start: float = 100.0) -> None:
        self.clock = FakeClock(start)
        self.exposure_s = exposure_s
        self.timelines = {
            key: FrameTimeline(self.clock).use_pixels([_texture(seed)])
            for key, seed in (("left", 1), ("right", 2))
        }
        park = FakeMountPark(start_parked=False)
        self.panel = MountTestMovePanel(
            FakeMountAdapter(),
            mount_park=park,
            get_left_frame=lambda: None,
            get_right_frame=lambda: None,
            wait_for_left_frame=self.timelines["left"].waiter(),
            wait_for_right_frame=self.timelines["right"].waiter(),
            get_left_exposure_gain=lambda: (exposure_s * 1000.0, 100),
            get_right_exposure_gain=lambda: (exposure_s * 1000.0, 100),
            settings=MountAlignmentSettings(
                frame_settle_ms=_FRAME_SETTLE_MS, stability_sample_interval_s=0.2
            ),
            tracking_enforcer=TrackingEnforcer(
                park, OperatingMode.TERRESTRIAL, settle_timeout_s=0, clock=self.clock
            ),
            runner=MountTestMoveRunner(clock=self.clock),
            clock=self.clock,
        )

    def stream(self, *, first_start: float, count: int = 400) -> None:
        for timeline in self.timelines.values():
            timeline.schedule_stream(
                first_start=first_start, exposure_s=self.exposure_s,
                cadence_s=self.exposure_s, count=count,
            )

    def accepted_starts(self) -> list[float]:
        """Exposure start of every frame the stability window took (the last N per camera are
        the measured ones; all must be fresh)."""
        evidence = self.panel._last_diagnostic_stability
        return [
            timing["exposure_start_monotonic"]
            for camera in evidence.values()
            for timing in camera.get("frame_timings", [])
        ]


class TestTheMotionReferenceIsOnTheInjectedClock:
    @pytest.mark.parametrize("exposure_s", [0.2, 2.0, 4.0])
    def test_frames_overlapping_the_motion_or_the_settle_are_never_measured(
        self, qapp: object, exposure_s: float
    ) -> None:
        rig = _Rig(exposure_s=exposure_s)
        motion_end = rig.clock.monotonic()
        # A stale backlog delivered before the motion ended, then the stream that kept
        # exposing through the motion: the in-flight frame overlaps it.
        for timeline in rig.timelines.values():
            timeline.add(FrameSpec(motion_end - 10.0, exposure_s), FrameSpec(motion_end - 5.0, 1.0))
        rig.stream(first_start=motion_end - 0.5 * exposure_s)

        result = rig.panel._capture_both("terrestrial", after_monotonic=motion_end)

        assert result is not None and set(result) == {"left", "right"}, (
            rig.panel._last_capture_failures
        )
        starts = rig.accepted_starts()
        assert starts
        assert min(starts) >= motion_end + _FRAME_SETTLE_MS / 1000.0
        assert any(f.overlaps_motion or f.spec.exposure_start < motion_end
                   for f in rig.timelines["left"].delivered)  # stale ones were seen and skipped
        record = rig.panel._capture_timeline[-1]
        assert record["reference_monotonic"] == pytest.approx(
            motion_end + _FRAME_SETTLE_MS / 1000.0
        )

    def test_a_long_exposure_gets_the_shared_exposure_scaled_budget(self, qapp: object) -> None:
        rig = _Rig(exposure_s=4.0)

        request = rig.panel._snapshot_capture_request("terrestrial", None, None, True)

        per_frame = DEFAULT_FRESH_FRAME_BUDGET.timeout_s(4.0)
        assert per_frame == pytest.approx(9.0)
        assert request.timeout_s == pytest.approx(per_frame * 3)  # 3-sample stability window

    def test_a_stream_that_never_delivers_a_fresh_frame_times_out_on_fake_time(
        self, qapp: object
    ) -> None:
        rig = _Rig(exposure_s=1.0)
        motion_end = rig.clock.monotonic()
        for timeline in rig.timelines.values():  # only frames that started before the motion
            timeline.add(FrameSpec(motion_end - 0.5, 1.0))

        result = rig.panel._capture_both("terrestrial", after_monotonic=motion_end)

        assert not result
        assert {f.diagnostics.get("capture_status") for f in
                rig.panel._last_capture_failures.values()} == {"exposure_overlapped_motion"}
        # The whole budget passed on the fake clock: max(stability_timeout_s, 3 x 3 s) per camera.
        assert rig.clock.monotonic() - motion_end == pytest.approx(2 * 9.0, abs=0.5)


class TestTheVerifiedReferenceMaxAgeIsOnTheInjectedClock:
    @pytest.mark.parametrize(("age_s", "reused"), [(59.9, True), (60.0, True), (60.1, False)])
    def test_the_reference_expires_after_60_s_of_clock_time(
        self, qapp: object, age_s: float, reused: bool
    ) -> None:
        rig = _Rig(exposure_s=0.2)
        rig.stream(first_start=rig.clock.monotonic(), count=2000)
        assert rig.panel._capture_both("terrestrial")  # a verified at-rest reference
        taken_at = rig.panel._verified_reference.taken_at  # type: ignore[union-attr]

        rig.clock.advance(taken_at + age_s - rig.clock.monotonic())

        assert (rig.panel._reusable_reference("terrestrial") is not None) is reused


class TestEveryMoveEndIsTheNextReference:
    """S6.7 review: the end of EVERY runner outcome -- including a move whose pending action
    was dropped by Stop, and a failed calibration's stranded return -- is the next BEFORE
    capture's motion reference. Before, only a pending calibration/nudge/screen move recorded
    it, so the next nudge's BEFORE used an older move's end and frames exposed during the
    stopped move could pass as fresh."""

    def _outcome_after_stop(self, rig: _Rig) -> float:
        panel = rig.panel
        panel._connect_button.setChecked(True)
        panel._last_pulse_completed_at = rig.clock.monotonic()  # an earlier, finished move
        rig.clock.advance(5.0)
        outcomes = [MountPulseOutcome(pulsed=False, error="stopped by the user")]
        panel._runner.take_latest = lambda: outcomes.pop() if outcomes else None  # type: ignore[method-assign]
        panel._pending = None  # Stop dropped the pending nudge; the move call ended later
        panel._poll()
        return rig.clock.monotonic()

    def test_a_stopped_moves_end_is_recorded(self, qapp: object) -> None:
        rig = _Rig(exposure_s=0.2)

        stopped_move_end = self._outcome_after_stop(rig)

        assert rig.panel._last_pulse_completed_at == pytest.approx(stopped_move_end)

    def test_the_next_nudge_measures_no_frame_from_before_its_own_move(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """USER DECISION (2026-10-10, S6.6b): a nudge takes no BEFORE capture at all (it used to
        wait for frames after the stopped move here); its #43 check captures only AFTER its OWN
        move completed, so no frame exposed during the stopped move can be evidence."""
        rig = _Rig(exposure_s=0.2)
        stopped_move_end = self._outcome_after_stop(rig)
        requested: list[float | None] = []
        monkeypatch.setattr(
            rig.panel, "_request_capture",
            lambda mode, *, label, after_monotonic, then: requested.append(after_monotonic),
        )

        rig.panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)

        assert requested == []  # sent at once -- no BEFORE capture
        assert rig.panel._reusable_reference("terrestrial") is None  # nothing verified to reuse
        assert rig.panel._last_pulse_completed_at == pytest.approx(stopped_move_end)
