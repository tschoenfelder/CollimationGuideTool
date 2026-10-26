"""Issue #56 review F2/F3: terrestrial autofocus sampling and exposure path.

F2 -- the extra same-position samples are DISTINCT fresh frames from the fresh-frame source
(never `get_frame()` re-reads of the one latest frame), identical deliveries count once.
F3 -- when clipped tiles carry the score, the exposure is stepped down before (and, if
needed, after) searching, always restored, and a still clipping-dominated result is reported
as `highlights_clipped`, never a bare SUCCESS.

The rig is synthetic and deterministic in its optimum: a scene of faint bars plus one glint
whose brightness scales with the exposure, defocused (Gaussian, sigma = |position - 500| / 20
px) before clipping at full scale, with fresh sensor noise in every frame."""

from __future__ import annotations

import time

import numpy as np
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.port import FocuserMoveResult
from astrotool_core.focus.terrestrial_focus_metric import (
    TerrestrialFocusEvidence,
    analyze_terrestrial_focus,
)
from astrotool_core.timing import FakeClock
from collimation_tool.application import autofocus_controller as controller_module
from collimation_tool.application.autofocus_controller import (
    AUTOFOCUS_FRESH_FRAME_BUDGET,
    HIGHLIGHTS_CLIPPED,
    AutofocusController,
    AutofocusMode,
    AutofocusResult,
    ExposureControl,
)
from collimation_tool.application.autofocus_search import AutofocusStatus

_SHAPE = (256, 256)
_BEST = 500
_FULL_SCALE = 65520.0
_NOISE = 300.0


class _GlintRig:
    def __init__(
        self, *, glint_at_100ms: float, min_exposure_ms: float = 1.0, start: int = 700
    ) -> None:
        self.focuser = FakeFocuser()
        self.focuser.move_absolute(start)
        self.exposure_ms, self.gain = 100.0, 100
        self.min_exposure_ms = min_exposure_ms
        self.set_calls: list[tuple[float, int]] = []
        self.wait_calls = 0
        self.get_frame_calls = 0
        #: (focuser position, exposure ms) of every delivered frame
        self.frames_log: list[tuple[int, float]] = []
        self._rng = np.random.default_rng(5)
        scene = np.full(_SHAPE, 20000.0)
        for column in (40, 72, 104, 136, 168):
            scene[64:192, column : column + 6] = 30000.0
        scene[220:228, 220:228] = glint_at_100ms
        self._spectrum = np.fft.rfft2(scene)
        self._f2 = (
            np.fft.fftfreq(_SHAPE[0])[:, None] ** 2 + np.fft.rfftfreq(_SHAPE[1])[None, :] ** 2
        )

    def image(self) -> np.ndarray:
        sigma = abs(self.focuser.get_position() - _BEST) / 20.0
        clean = np.fft.irfft2(
            self._spectrum * np.exp(-2.0 * (np.pi * sigma) ** 2 * self._f2), s=_SHAPE
        )
        noisy = clean * (self.exposure_ms / 100.0) + self._rng.normal(0.0, _NOISE, _SHAPE)
        return np.minimum(noisy, _FULL_SCALE)

    def wait_for_frame(self, _reference: float, _timeout_s: float) -> FrameAcquisitionResult:
        self.wait_calls += 1
        self.frames_log.append((self.focuser.get_position(), self.exposure_ms))
        return FrameAcquisitionResult(
            status=FrameAcquisitionStatus.OK,
            frame=DeliveredFrame(
                pixels=self.image(), captured_at_monotonic=time.monotonic(), exposure_seconds=0.1
            ),
        )

    def get_frame(self) -> np.ndarray:
        self.get_frame_calls += 1
        return self.image()

    def exposure_control(self, settle_s: float = 0.5) -> ExposureControl:
        def apply(exposure_ms: float, gain: int) -> None:
            self.set_calls.append((exposure_ms, gain))
            self.exposure_ms, self.gain = max(exposure_ms, self.min_exposure_ms), gain

        return ExposureControl(
            get=lambda: (self.exposure_ms, self.gain), set=apply, settle_s=settle_s
        )

    def controller(
        self, *, exposure: bool = True, clock: FakeClock | None = None
    ) -> AutofocusController:
        return AutofocusController(
            self.focuser,
            get_frame=self.get_frame,
            wait_for_frame=self.wait_for_frame,
            set_auto_exposure_paused=lambda _paused: None,
            coarse_step=100,
            fine_step=10,
            exposure_control=self.exposure_control() if exposure else None,
            clock=clock or FakeClock(),
        )

    def run(self, *, exposure: bool = True, clock: FakeClock | None = None) -> AutofocusResult:
        return self.controller(exposure=exposure, clock=clock).run(AutofocusMode.TERRESTRIAL)


class TestDistinctFreshSamples:
    def test_every_sample_is_a_fresh_frame_never_a_get_frame_re_read(self) -> None:
        rig = _GlintRig(glint_at_100ms=40000.0)

        result = rig.run(exposure=False)

        assert result.status is AutofocusStatus.SUCCESS
        assert rig.get_frame_calls == 0
        # three fresh frames for every measured position
        assert rig.wait_calls == 3 * len(result.samples)

    def test_an_identical_re_delivered_frame_counts_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[int] = []

        def spy(frames: list[np.ndarray], *, tile_size_px: int = 64) -> TerrestrialFocusEvidence:
            seen.append(len(frames))
            return analyze_terrestrial_focus(frames, tile_size_px=tile_size_px)

        monkeypatch.setattr(controller_module, "analyze_terrestrial_focus", spy)
        frame = np.full(_SHAPE, 1000.0)
        frame[64:192, 100:110] = 9000.0
        focuser = FakeFocuser()

        def same_frame(_reference: float, _timeout_s: float) -> FrameAcquisitionResult:
            return FrameAcquisitionResult(
                status=FrameAcquisitionStatus.OK,
                frame=DeliveredFrame(
                    pixels=frame, captured_at_monotonic=time.monotonic(), exposure_seconds=0.1
                ),
            )

        AutofocusController(
            focuser, get_frame=lambda: frame, wait_for_frame=same_frame,
            set_auto_exposure_paused=lambda _p: None, coarse_step=100, fine_step=10,
            clock=FakeClock(),
        ).run(AutofocusMode.TERRESTRIAL)

        assert seen and set(seen) == {1}

    def test_a_failed_extra_wait_still_measures_with_the_frames_it_has(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Review 2 #3: the third sample of the first position never arrives -- every wait
        # slice of that one acquisition times out (a real waiter blocks for its slice: the
        # fake clock advances by it), so the fresh-frame wait gives up after its budget.
        rig = _GlintRig(glint_at_100ms=40000.0)
        clock = FakeClock()
        seen: list[int] = []

        def spy(frames: list[np.ndarray], *, tile_size_px: int = 64) -> TerrestrialFocusEvidence:
            seen.append(len(frames))
            return analyze_terrestrial_focus(frames, tile_size_px=tile_size_px)

        monkeypatch.setattr(controller_module, "analyze_terrestrial_focus", spy)
        delivered = [0]
        dead: dict[str, float] = {}

        def third_sample_never_arrives(
            reference: float, timeout_s: float
        ) -> FrameAcquisitionResult:
            if delivered[0] == 2 and dead.get("reference", reference) == reference:
                dead.setdefault("reference", reference)
                dead.setdefault("started", clock.monotonic())
                clock.advance(timeout_s)
                dead["ended"] = clock.monotonic()
                return FrameAcquisitionResult(status=FrameAcquisitionStatus.TIMEOUT, frame=None)
            delivered[0] += 1
            return rig.wait_for_frame(reference, timeout_s)

        result = AutofocusController(
            rig.focuser, get_frame=rig.get_frame, wait_for_frame=third_sample_never_arrives,
            set_auto_exposure_paused=lambda _p: None, coarse_step=100, fine_step=10,
            clock=clock,
        ).run(AutofocusMode.TERRESTRIAL)

        assert seen[0] == 2  # the first position was measured with the two frames it got
        assert set(seen[1:]) == {3}
        # that one failed wait cost exactly one fresh-frame budget (exposure learned: 0.1 s)
        assert dead["ended"] - dead["started"] == pytest.approx(
            AUTOFOCUS_FRESH_FRAME_BUDGET.timeout_s(0.1)
        )
        assert result.status is AutofocusStatus.SUCCESS
        assert abs((result.best_position or 0) - _BEST) <= 100

    def test_a_stop_during_an_extra_wait_cancels_the_run(self) -> None:
        rig = _GlintRig(glint_at_100ms=40000.0)
        calls = [0]

        def second_wait_cancelled(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            calls[0] += 1
            if calls[0] == 2:
                return FrameAcquisitionResult(status=FrameAcquisitionStatus.CANCELLED, frame=None)
            return rig.wait_for_frame(reference, timeout_s)

        result = AutofocusController(
            rig.focuser, get_frame=rig.get_frame, wait_for_frame=second_wait_cancelled,
            set_auto_exposure_paused=lambda _p: None, coarse_step=100, fine_step=10,
            clock=FakeClock(),
        ).run(AutofocusMode.TERRESTRIAL)

        assert result.status is AutofocusStatus.CANCELLED


class TestClippedHighlightsStepDown:
    def test_a_clipping_dominated_start_steps_the_exposure_down_before_searching(self) -> None:
        # The glint is 1.5x full scale at 100 ms (clipped, carrying the score) and 0.75x at 50.
        rig = _GlintRig(glint_at_100ms=1.5 * _FULL_SCALE)

        result = rig.run()

        assert result.exposure_attempts == ((100.0, 100), (50.0, 100))
        assert result.status is AutofocusStatus.SUCCESS
        assert abs((result.best_position or 0) - _BEST) <= 100
        assert rig.exposure_ms == 100.0  # restored
        assert rig.set_calls[-1] == (100.0, 100)

    def test_an_unclipped_scene_is_never_stepped_down(self) -> None:
        rig = _GlintRig(glint_at_100ms=40000.0)

        result = rig.run()

        assert result.status is AutofocusStatus.SUCCESS
        assert result.exposure_attempts == ((100.0, 100),)
        assert rig.set_calls == []

    def test_still_clipping_after_every_step_is_reported_never_success(self) -> None:
        rig = _GlintRig(glint_at_100ms=50.0 * _FULL_SCALE)

        result = rig.run()

        assert result.status is AutofocusStatus.INSUFFICIENT_EVIDENCE
        assert result.failure_reason == HIGHLIGHTS_CLIPPED
        assert len(result.exposure_attempts) == 3  # max_exposure_attempts
        assert rig.exposure_ms == 100.0

    def test_at_the_cameras_minimum_exposure_it_is_reported(self) -> None:
        rig = _GlintRig(glint_at_100ms=1.5 * _FULL_SCALE, min_exposure_ms=100.0)

        result = rig.run()

        assert result.status is AutofocusStatus.INSUFFICIENT_EVIDENCE
        assert result.failure_reason == HIGHLIGHTS_CLIPPED
        assert rig.exposure_ms == 100.0

    def test_without_exposure_control_a_clipping_dominated_run_is_reported(self) -> None:
        rig = _GlintRig(glint_at_100ms=1.5 * _FULL_SCALE)

        result = rig.run(exposure=False)

        assert result.status is AutofocusStatus.INSUFFICIENT_EVIDENCE
        assert result.failure_reason == HIGHLIGHTS_CLIPPED
        assert rig.set_calls == []

    def test_each_step_waits_the_settle_time_on_the_injected_clock(self) -> None:
        clock = FakeClock()
        rig = _GlintRig(glint_at_100ms=1.5 * _FULL_SCALE)

        rig.run(clock=clock)

        assert 0.5 in clock.sleeps

    def test_a_stop_during_the_settle_cancels_and_restores(self) -> None:
        import threading

        rig = _GlintRig(glint_at_100ms=1.5 * _FULL_SCALE)
        stop = threading.Event()
        clock = FakeClock()
        clock.call_later(0.1, stop.set)
        controller = rig.controller(clock=clock)

        result = controller.run(AutofocusMode.TERRESTRIAL, cancel_event=stop)

        assert result.status is AutofocusStatus.CANCELLED
        assert rig.exposure_ms == 100.0

    def test_star_mode_never_touches_the_exposure(self) -> None:
        rig = _GlintRig(glint_at_100ms=1.5 * _FULL_SCALE)

        rig.controller().run(AutofocusMode.STAR)

        assert rig.set_calls == []


class TestClippingNeverMasksAFailure:
    """Review 2 #1: a refused move or a dead camera stream is reported as such, even with a
    50x full-scale glint in the scene -- never as 'lower the exposure'."""

    def test_a_refused_move_is_reported_as_move_rejected(self) -> None:
        rig = _GlintRig(glint_at_100ms=50.0 * _FULL_SCALE)
        real_move = rig.focuser.move_absolute
        moves = [0]

        def fourth_move_refused(steps: int) -> FocuserMoveResult:
            moves[0] += 1
            if moves[0] > 3:
                return FocuserMoveResult(False, steps, rig.focuser.get_position())
            return real_move(steps)

        rig.focuser.move_absolute = fourth_move_refused  # type: ignore[method-assign]

        result = rig.run()

        assert result.status is AutofocusStatus.MOVE_REJECTED
        assert result.failure_reason != HIGHLIGHTS_CLIPPED
        assert rig.exposure_ms == 100.0

    def test_a_dead_stream_is_reported_as_frame_acquisition_failed(self) -> None:
        rig = _GlintRig(glint_at_100ms=50.0 * _FULL_SCALE)
        clock = FakeClock()
        delivered = [0]

        def dies_after_13_frames(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            if delivered[0] >= 13:
                clock.advance(timeout_s)  # a real waiter blocks for its slice
                return FrameAcquisitionResult(status=FrameAcquisitionStatus.TIMEOUT, frame=None)
            delivered[0] += 1
            return rig.wait_for_frame(reference, timeout_s)

        result = AutofocusController(
            rig.focuser, get_frame=rig.get_frame, wait_for_frame=dies_after_13_frames,
            set_auto_exposure_paused=lambda _p: None, coarse_step=100, fine_step=10,
            exposure_control=rig.exposure_control(), clock=clock,
        ).run(AutofocusMode.TERRESTRIAL)

        assert result.status is AutofocusStatus.FRAME_ACQUISITION_FAILED
        assert (result.failure_reason or "").startswith("fresh_frame_timeout")
        assert rig.exposure_ms == 100.0


class _BarsAndGlintsRig(_GlintRig):
    """Review 2 #2 (the reviewer's rig): eleven bars plus three 6x6 glints at 3x full scale.
    At true focus the glints clip and carry the whole score; one coarse step either side
    they fall just below full scale, their halo makes the neighbour score slightly higher,
    and nothing there is clipped -- a check of the best sample alone sees no clipping."""

    def __init__(self, *, start: int) -> None:
        super().__init__(glint_at_100ms=0.0, start=start)
        shape = (512, 512)
        scene = np.full(shape, 20000.0)
        for column in range(40, 480, 48):
            scene[64:448, column : column + 6] = 26000.0
        for y, x in ((100, 60), (300, 252), (420, 400)):
            scene[y : y + 6, x : x + 6] = 3.0 * _FULL_SCALE
        self._shape = shape
        self._spectrum = np.fft.rfft2(scene)
        self._f2 = np.fft.fftfreq(shape[0])[:, None] ** 2 + np.fft.rfftfreq(shape[1])[None, :] ** 2

    def image(self) -> np.ndarray:
        sigma = abs(self.focuser.get_position() - _BEST) / 20.0
        scale = self.exposure_ms / 100.0
        clean = np.fft.irfft2(
            self._spectrum * np.exp(-2.0 * (np.pi * sigma) ** 2 * self._f2), s=self._shape
        )
        noisy = clean * scale + self._rng.normal(0.0, _NOISE * np.sqrt(scale), self._shape)
        return np.minimum(noisy, _FULL_SCALE)


class TestClippedFocusNextToAnUnclippedNeighbour:
    @pytest.mark.parametrize("start", [200, 300, 700, 800])
    def test_the_clipped_true_focus_is_seen_and_stepped_down(self, start: int) -> None:
        rig = _BarsAndGlintsRig(start=start)

        result = rig.run()

        assert len(result.exposure_attempts) >= 2  # the clipping near the best was noticed
        assert result.status is AutofocusStatus.SUCCESS
        assert abs((result.best_position or 0) - _BEST) <= 10
        assert rig.exposure_ms == 100.0


class TestUntrustedBestIsNotKept:
    def test_a_highlights_clipped_result_returns_the_focuser_to_its_start(self) -> None:
        # Review 2 #4: same as the searcher's own insufficient-evidence path.
        rig = _GlintRig(glint_at_100ms=50.0 * _FULL_SCALE, start=700)

        result = rig.run()

        assert result.failure_reason == HIGHLIGHTS_CLIPPED
        assert rig.focuser.get_position() == 700


class TestExposureRestoreRace:
    def test_a_queued_lowering_is_restored_even_when_a_stop_ends_the_settle(self) -> None:
        # Review 2 #5: the camera applies the new exposure later (queued to the UI thread),
        # so reading it back during the settle still shows the original value.
        import threading

        rig = _GlintRig(glint_at_100ms=1.5 * _FULL_SCALE)
        issued: list[tuple[float, int]] = []
        queued = ExposureControl(
            get=lambda: (100.0, 100), set=lambda ms, gain: issued.append((ms, gain)),
            settle_s=0.5,
        )
        stop = threading.Event()
        clock = FakeClock()
        clock.call_later(0.1, stop.set)

        result = AutofocusController(
            rig.focuser, get_frame=rig.get_frame, wait_for_frame=rig.wait_for_frame,
            set_auto_exposure_paused=lambda _p: None, coarse_step=100, fine_step=10,
            exposure_control=queued, clock=clock,
        ).run(AutofocusMode.TERRESTRIAL, cancel_event=stop)

        assert result.status is AutofocusStatus.CANCELLED
        assert issued[0] == (50.0, 100)
        assert issued[-1] == (100.0, 100)


class TestPrecheckTakesEverySteps:
    def test_the_precheck_alone_steps_down_up_to_max_exposure_attempts(self) -> None:
        # Review 2 #6: the glint (30x full scale, defocused at the start) still clips at
        # 100 and 50 ms there and only stops at 25 ms -- all three attempts are taken at the
        # start position, so the search never measures away from it above 25 ms.
        rig = _GlintRig(glint_at_100ms=30.0 * _FULL_SCALE, start=700)

        result = rig.run()

        assert [ms for ms, _gain in result.exposure_attempts] == [100.0, 50.0, 25.0]
        away = {ms for position, ms in rig.frames_log if position != 700}
        assert away == {25.0}


class TestAFailedSearchIsNotRetried:
    @pytest.mark.parametrize("refused_from_move", [4, 5, 6])
    def test_a_search_ended_by_a_refused_move_never_triggers_a_step_down(
        self, refused_from_move: int
    ) -> None:
        # Review 2 #1: the start is unclipped (no precheck step); the search reaches the
        # clipped focus, then the focuser refuses a move. That failure must not be turned
        # into an exposure step-down and a second search.
        rig = _GlintRig(glint_at_100ms=1.5 * _FULL_SCALE, start=700)
        real_move = rig.focuser.move_absolute
        moves = [0]

        def refuses_later(steps: int) -> FocuserMoveResult:
            moves[0] += 1
            if moves[0] >= refused_from_move:
                return FocuserMoveResult(False, steps, rig.focuser.get_position())
            return real_move(steps)

        rig.focuser.move_absolute = refuses_later  # type: ignore[method-assign]

        result = rig.run()

        assert result.status is AutofocusStatus.MOVE_REJECTED
        assert rig.set_calls == []
