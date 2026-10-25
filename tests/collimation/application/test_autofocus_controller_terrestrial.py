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
from astrotool_core.focus.terrestrial_focus_metric import (
    TerrestrialFocusEvidence,
    analyze_terrestrial_focus,
)
from astrotool_core.timing import FakeClock
from collimation_tool.application import autofocus_controller as controller_module
from collimation_tool.application.autofocus_controller import (
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

    def test_a_failed_extra_wait_still_measures_with_the_frames_it_has(self) -> None:
        rig = _GlintRig(glint_at_100ms=40000.0)
        calls = [0]

        def every_third_times_out(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            calls[0] += 1
            if calls[0] % 3 == 0:
                return FrameAcquisitionResult(status=FrameAcquisitionStatus.TIMEOUT, frame=None)
            return rig.wait_for_frame(reference, timeout_s)

        result = AutofocusController(
            rig.focuser, get_frame=rig.get_frame, wait_for_frame=every_third_times_out,
            set_auto_exposure_paused=lambda _p: None, coarse_step=100, fine_step=10,
            clock=FakeClock(),
        ).run(AutofocusMode.TERRESTRIAL)

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
