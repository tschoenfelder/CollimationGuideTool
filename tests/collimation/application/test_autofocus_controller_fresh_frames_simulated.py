"""S6.7 (T05, #52): autofocus measures only frames whose exposure STARTED after the focuser move
ended, under the shared exposure-scaled deadline -- not the fixed 5 s that made any exposure
above ~2 s on a STREAMING camera fail as `frame_acquisition_failed`.

The production `OnStepFocuserAdapter` runs on the #51 OnStep simulator; the camera is a
`FrameTimeline` stream of back-to-back exposures on the same FakeClock (the real ToupTek stream
keeps exposing while the focuser moves). The star's blur follows the simulated focuser's
optical position at exposure start; a frame that started while the focuser was still moving is
marked as such and must never be scored."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest
from astrotool_core.acquisition import FrameAcquisitionResult, FrameAcquisitionStatus
from astrotool_core.focus.port import FocuserMoveResult
from astrotool_core.onstep import OnStepFocuserAdapter
from astrotool_core.testing import (
    FocuserScenario,
    FrameTimeline,
    OnStepScenario,
    make_simulated_onstep_connection,
    single_star_image,
)
from astrotool_core.timing import FakeClock
from collimation_tool.application.autofocus_controller import AutofocusController, AutofocusMode
from collimation_tool.application.autofocus_search import AutofocusStatus

_BEST = 5300


class _Rig:
    def __init__(self, *, exposure_s: float, stream: bool = True) -> None:
        self.clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(focuser=FocuserScenario(position=5000, steps_per_s=400.0)),
            clock=self.clock,
        )
        self.focuser = OnStepFocuserAdapter(connection)
        self.focuser.connect()
        self.sim_focuser = made[0].focuser
        #: (start, end) clock time of every focuser motion (end = first poll seeing it stopped).
        self.motions: list[tuple[float, float]] = []
        self._moving_since: float | None = None
        move, is_moving = self.focuser.move_absolute, self.focuser.is_moving

        def recording_move(position: int) -> FocuserMoveResult:
            self._moving_since = self.clock.monotonic()
            return move(position)

        def recording_is_moving() -> bool:
            moving = is_moving()
            if not moving and self._moving_since is not None:
                self.motions.append((self._moving_since, self.clock.monotonic()))
                self._moving_since = None
            return moving

        # Instance attributes shadow the adapter's methods (recording only, same behaviour).
        setattr(self.focuser, "move_absolute", recording_move)  # noqa: B010
        setattr(self.focuser, "is_moving", recording_is_moving)  # noqa: B010
        self.timeline = FrameTimeline(self.clock, render=self._scene)
        if stream:
            self.timeline.schedule_stream(
                first_start=0.0, exposure_s=exposure_s, cadence_s=exposure_s, count=4000
            )
        #: Exposure start of every frame the controller was handed (OK results).
        self.scored_starts: list[tuple[float, float]] = []

    def _scene(self, _start: float, _end: float) -> np.ndarray:
        optical = self.sim_focuser.optical_position or 0.0
        sigma = 1.5 + abs(optical - _BEST) / 120.0
        return single_star_image(
            (64, 96), x=48.0, y=32.0, peak=6000.0 / (sigma / 1.5) ** 2, sigma=sigma
        )

    def wait_for_frame(self, reference: float, timeout_s: float) -> FrameAcquisitionResult:
        result = self.timeline.waiter()(reference, timeout_s)
        if result.ok and result.frame is not None:
            begin = result.frame.captured_at_monotonic - result.frame.exposure_seconds
            self.scored_starts.append((begin, self.clock.monotonic()))
        return result

    def run(self, cancel_check: Callable[[], bool] | None = None) -> object:
        controller = AutofocusController(
            self.focuser,
            get_frame=lambda: None,
            wait_for_frame=self.wait_for_frame,
            set_auto_exposure_paused=lambda _paused: None,
            star_edge_margin_px=8,
            clock=self.clock,
        )
        return controller.run(AutofocusMode.STAR, cancel_check)

    def stale_scores(self) -> list[tuple[float, float, float]]:
        stale = []
        for begin, used_at in self.scored_starts:
            ended = [end for _s, end in self.motions if end <= used_at]
            if ended and begin < max(ended):
                stale.append((begin, used_at, max(ended)))
        return stale


class TestStreamingExposures:
    @pytest.mark.parametrize("exposure_s", [0.5, 1.0, 3.0, 4.0])
    def test_focus_is_found_and_no_scored_frame_overlapped_a_focuser_move(
        self, exposure_s: float
    ) -> None:
        rig = _Rig(exposure_s=exposure_s)

        result = rig.run()

        assert result.status is AutofocusStatus.SUCCESS, result  # type: ignore[attr-defined]
        assert abs(result.best_position - _BEST) <= 50  # type: ignore[attr-defined]
        assert rig.motions, "the focuser never moved"
        assert rig.stale_scores() == []
        # Frames DID overlap moves on this stream (they were rejected, not avoided).
        assert any(
            any(s < end and s + exposure_s > begin for begin, end in rig.motions)
            for s in (f.spec.exposure_start for f in rig.timeline.delivered)
        )


class TestExplicitTimeout:
    def test_a_camera_that_never_delivers_is_an_explicit_frame_timeout(self) -> None:
        rig = _Rig(exposure_s=1.0, stream=False)  # nothing is ever delivered

        result = rig.run()

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE  # type: ignore[attr-defined]
        reason = result.failure_reason  # type: ignore[attr-defined]
        assert reason is not None and reason.startswith("fresh_frame_timeout")
        assert "no frame arrived" in reason
        assert rig.clock.monotonic() == pytest.approx(5.0)  # the assumed-2 s budget, then stop

    def test_a_stream_that_stops_after_a_focuser_move_is_an_explicit_frame_timeout(
        self,
    ) -> None:
        rig = _Rig(exposure_s=0.5)
        waiter = rig.wait_for_frame

        def dead_after_first_move(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            if rig.motions:
                rig.clock.sleep(timeout_s)
                return FrameAcquisitionResult(FrameAcquisitionStatus.TIMEOUT)
            return waiter(reference, timeout_s)

        rig.wait_for_frame = dead_after_first_move  # type: ignore[method-assign]

        result = rig.run()

        assert result.status is AutofocusStatus.FRAME_ACQUISITION_FAILED  # type: ignore[attr-defined]
        reason = result.failure_reason  # type: ignore[attr-defined]
        assert reason is not None and reason.startswith("fresh_frame_timeout")
        # Scaled from the 0.5 s exposure seen before the move: max(2 s, 2 x 0.5 + 1) = 2 s.
        assert "of 2.0 s" in reason and "exposure 0.5 s" in reason

    def test_an_unknown_exposure_longer_than_the_assumed_budget_is_explicit_not_stale(
        self,
    ) -> None:
        """Known limit (no exposure hint is wired, #49: no widget read on the run thread): a
        stream whose FIRST frame needs > 5 s ends the run explicitly, never with a stale frame.
        Exposures up to ~5 s are learned from the in-flight frame (see above)."""
        rig = _Rig(exposure_s=6.0)

        result = rig.run()

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE  # type: ignore[attr-defined]
        reason = result.failure_reason  # type: ignore[attr-defined]
        assert reason is not None and reason.startswith("fresh_frame_timeout")
        assert rig.scored_starts == []


class TestStop:
    def test_stop_during_a_long_frame_wait_cancels_within_one_slice(self) -> None:
        """S6.7 review: with a 20 s stream the first frame is far away; Stop at t=3 s ends the
        wait within one 0.5 s slice and the run reports CANCELLED, not a frame failure."""
        rig = _Rig(exposure_s=20.0)
        stop_at = 3.0

        result = rig.run(cancel_check=lambda: rig.clock.monotonic() >= stop_at)

        assert result.status is AutofocusStatus.CANCELLED  # type: ignore[attr-defined]
        assert stop_at <= rig.clock.monotonic() <= stop_at + 0.5 + 1e-9
