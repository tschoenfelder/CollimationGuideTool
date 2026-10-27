"""Issue #58: artificial-star autofocus with SEVERAL heavily over-exposed stars (diagnostic
4c5677d0: four clipped blobs at 2000 ms, `no_usable_evidence` / `multiple_candidates` on the
first frame, the focuser never moved).

Simulated sweep on a FakeClock: broad Gaussian stars clipped at 65520 at the start exposure.
Star width and brightness are pure functions of the focuser position (best focus at `_BEST`,
sigma 8 px there and +1.6 px per 100 steps away) and of the exposure (peak proportional to it,
flux conserved) -- the expected optimum and the star to follow (the one nearest the image
centre, NOT the brightest) are known independently of the code under test."""

from __future__ import annotations

import threading

import numpy as np
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.star_candidates import find_star_candidates
from astrotool_core.focus.star_focus_metric import StarTargetTracker
from astrotool_core.testing.frame_factory import StarSpec, star_field_image
from astrotool_core.timing import FakeClock
from collimation_tool.application.autofocus_controller import (
    SATURATED_AT_MIN_EXPOSURE,
    AutofocusController,
    AutofocusMode,
    AutofocusResult,
    ExposureControl,
)
from collimation_tool.application.autofocus_search import AutofocusStatus

_SHAPE = (480, 640)  # (height, width): centre (319.5, 239.5)
_BEST = 500
_START = 700
_START_EXPOSURE_MS = 2000.0
_FULL_SCALE = 65520.0
_BACKGROUND = 1000.0
#: Peak (ADU above background) of a brightness-1 star at best focus and the start exposure.
_PEAK_AT_FOCUS = 4.0e6
_SIGMA_AT_FOCUS = 8.0
#: The target (brightness 1): nearest the centre but the FAINTEST of the four.
_TARGET = (350.0, 256.0)
_FOUR_STARS = ((350.0, 256.0, 1.0), (120.0, 110.0, 1.5), (530.0, 110.0, 1.3), (500.0, 370.0, 1.2))


class _Rig:
    def __init__(
        self,
        focuser: FakeFocuser,
        clock: FakeClock,
        stars: tuple[tuple[float, float, float], ...] = _FOUR_STARS,
        *,
        sigma_at_focus: float = _SIGMA_AT_FOCUS,
        sigma_per_step: float = 0.016,
        peak_at_focus: float = _PEAK_AT_FOCUS,
        background: float = _BACKGROUND,
        clip: float = _FULL_SCALE,
        hot_pixels: int = 0,
    ) -> None:
        self.focuser = focuser
        self.clock = clock
        self.stars = stars
        self.sigma_at_focus = sigma_at_focus
        self.sigma_per_step = sigma_per_step
        self.peak_at_focus = peak_at_focus
        self.background = background
        self.clip = clip
        self.exposure_ms = _START_EXPOSURE_MS
        self.gain = 460
        self.min_exposure_ms = 0.1
        self.frames_served = 0
        self._rng = np.random.default_rng(58)
        hot = np.random.default_rng(7)
        self._hot = (hot.integers(0, _SHAPE[0], hot_pixels), hot.integers(0, _SHAPE[1], hot_pixels))

    def sigma(self) -> float:
        return self.sigma_at_focus + self.sigma_per_step * abs(self.focuser.get_position() - _BEST)

    def peak(self, brightness: float = 1.0) -> float:
        """The star's true peak above the background (before clipping)."""
        scale = (self.exposure_ms / _START_EXPOSURE_MS) * (self.sigma_at_focus / self.sigma()) ** 2
        return self.peak_at_focus * brightness * scale

    def image(self) -> np.ndarray:
        sigma = self.sigma()
        stars = [StarSpec(x=x, y=y, peak=self.peak(b), sigma=sigma) for x, y, b in self.stars]
        image = star_field_image(_SHAPE, stars, background=self.background).astype(np.float64)
        image += self._rng.normal(0.0, 30.0, _SHAPE)
        image[self._hot] = self.clip  # hot pixels: clipped whatever the exposure
        return np.clip(np.round(image), 0.0, self.clip)

    def wait_for_frame(
        self, reference_monotonic: float, timeout_s: float
    ) -> FrameAcquisitionResult:
        self.frames_served += 1
        return FrameAcquisitionResult(
            status=FrameAcquisitionStatus.OK,
            frame=DeliveredFrame(
                pixels=self.image(),
                captured_at_monotonic=self.clock.monotonic(),
                exposure_seconds=self.exposure_ms / 1000.0,
            ),
        )

    def exposure_control(self) -> ExposureControl:
        def set_values(exposure_ms: float, gain: int) -> None:
            self.exposure_ms = max(exposure_ms, self.min_exposure_ms)
            self.gain = gain

        return ExposureControl(
            get=lambda: (self.exposure_ms, self.gain), set=set_values, settle_s=0.5
        )


def _controller(
    rig: _Rig, focuser: FakeFocuser, clock: FakeClock, **kwargs: object
) -> AutofocusController:
    options: dict[str, object] = {"coarse_step": 100, "fine_step": 10, **kwargs}
    return AutofocusController(
        focuser,
        get_frame=rig.image,
        wait_for_frame=rig.wait_for_frame,
        set_auto_exposure_paused=lambda paused: None,
        exposure_control=rig.exposure_control(),
        clock=clock,
        **options,  # type: ignore[arg-type]
    )


def _run(
    *,
    start: int = _START,
    min_exposure_ms: float = 0.1,
    stars: tuple[tuple[float, float, float], ...] = _FOUR_STARS,
    rig_options: dict[str, float] | None = None,
    start_exposure_ms: float = _START_EXPOSURE_MS,
    **controller_kwargs: object,
) -> tuple[_Rig, FakeFocuser, FakeClock, AutofocusResult]:
    clock = FakeClock()
    focuser = FakeFocuser()
    focuser.move_absolute(start)
    rig = _Rig(focuser, clock, stars, **(rig_options or {}))  # type: ignore[arg-type]
    rig.min_exposure_ms = min_exposure_ms
    rig.exposure_ms = start_exposure_ms
    result = _controller(rig, focuser, clock, **controller_kwargs).run(
        AutofocusMode.ARTIFICIAL_STAR
    )
    return rig, focuser, clock, result


def _assert_halvings_from_the_start(result: AutofocusResult) -> list[float]:
    exposures = [exposure for exposure, _gain in result.exposure_attempts]
    assert exposures[0] == _START_EXPOSURE_MS
    assert all(b == a / 2 for a, b in zip(exposures, exposures[1:], strict=False))
    return exposures


class TestSeveralOverExposedStars:
    def test_the_sweep_steps_down_converges_on_the_star_nearest_the_centre_and_restores(
        self,
    ) -> None:
        rig, _focuser, clock, result = _run()

        assert result.status is AutofocusStatus.SUCCESS, result  # was multiple_candidates
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 10
        # The star nearest the centre (the faintest of the four) was followed and reported.
        assert result.tracked_target is not None
        assert abs(result.tracked_target[0] - _TARGET[0]) < 3.0
        assert abs(result.tracked_target[1] - _TARGET[1]) < 3.0
        assert result.target_candidates == 4
        # The exposure was halved from the start until the star was unclipped (more than
        # the 2 halvings max_exposure_attempts=3 allows) ...
        exposures = _assert_halvings_from_the_start(result)
        assert len(exposures) >= 4
        # ... each lowering waited its settle time on the injected clock ...
        assert clock.sleeps.count(0.5) == len(exposures) - 1
        # ... and the original exposure/gain is back afterwards.
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)

    @pytest.mark.parametrize("start", [900, 1100, 1300])
    def test_far_defocused_starts_converge_on_best_focus(self, start: int) -> None:
        # Review F1/F2: from 1100/1300 the old pipeline found no star at all (detect_sources
        # dropped the big blob); from 900 the old 37x37 moment FWHM was flat and reported a
        # false ALREADY_FOCUSED.
        rig, _focuser, _clock, result = _run(start=start)

        assert result.status is AutofocusStatus.SUCCESS, result
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 10
        assert result.tracked_target is not None
        assert abs(result.tracked_target[0] - _TARGET[0]) < 3.0
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)

    def test_the_camera_minimum_exposure_ends_it_as_saturated_at_min_exposure(self) -> None:
        rig, focuser, _clock, result = _run(min_exposure_ms=250.0)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == SATURATED_AT_MIN_EXPOSURE
        assert _assert_halvings_from_the_start(result)[-1] == 250.0
        assert focuser.get_position() == _START  # never searched on a clipped star
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)
        rig.exposure_ms = 250.0
        assert rig.peak() > 3 * _FULL_SCALE  # by construction still clipped at the minimum

    def test_the_step_cap_ends_it_as_saturated_at_min_exposure(self) -> None:
        rig, focuser, _clock, result = _run(max_acquisition_exposure_steps=3)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == SATURATED_AT_MIN_EXPOSURE
        assert _assert_halvings_from_the_start(result) == [2000.0, 1000.0, 500.0, 250.0]
        assert focuser.get_position() == _START
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)

    def test_a_stop_during_the_step_down_ends_the_run_cancelled_and_restores(self) -> None:
        stop = threading.Event()

        class _StopAtSecondSettle(FakeClock):
            def sleep(self, seconds: float, cancel: threading.Event | None = None) -> bool:
                if seconds == 0.5 and self.sleeps.count(0.5) == 1:
                    stop.set()
                return super().sleep(seconds, cancel)

        clock = _StopAtSecondSettle()
        focuser = FakeFocuser()
        focuser.move_absolute(_START)
        rig = _Rig(focuser, clock)

        result = _controller(rig, focuser, clock).run(
            AutofocusMode.ARTIFICIAL_STAR, cancel_event=stop
        )

        assert result.status is AutofocusStatus.CANCELLED
        assert focuser.get_position() == _START
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)


class TestStarsMergedWhileClipped:
    """Review F3: two stars 50 px apart whose clipped plateaus merge at the start exposure are
    ONE blob, centred between them; the target must be re-acquired once they are unclipped,
    never the merged blob's position followed into whichever star is nearer to it."""

    _PAIR = ((300.0, 240.0, 1.0), (350.0, 240.0, 1.0))  # 19.5 / 30.5 px from the centre

    def test_the_star_nearest_the_centre_is_followed_after_the_step_down(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(_START)
        merged = find_star_candidates(_Rig(focuser, FakeClock(), self._PAIR).image())
        assert len(merged) == 1 and merged[0].saturated  # precondition: one clipped blob
        assert abs(merged[0].x - 325.0) < 5.0

        rig, _focuser, _clock, result = _run(stars=self._PAIR)

        assert result.status is AutofocusStatus.SUCCESS, result
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 10
        assert result.tracked_target is not None
        assert abs(result.tracked_target[0] - 300.0) < 3.0  # not the merged 325
        assert result.target_candidates == 2
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)

    def test_two_equally_near_stars_are_not_guessed_after_the_step_down(self) -> None:
        pair = ((294.5, 239.5, 1.0), (344.5, 239.5, 1.0))  # both 25 px from the centre

        rig, focuser, _clock, result = _run(stars=pair)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == "multiple_candidates"
        assert focuser.get_position() == _START
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)


class TestAnUnclippedStartTakesNoStepDown:
    def test_no_exposure_change_and_no_extra_frame_when_the_star_is_not_clipped(self) -> None:
        clock = FakeClock()
        focuser = FakeFocuser()
        focuser.move_absolute(_START)
        rig = _Rig(focuser, clock)
        rig.exposure_ms = 15.625  # 2000 / 128: unclipped everywhere in the sweep

        result = _controller(rig, focuser, clock).run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.SUCCESS
        assert result.exposure_attempts == ((15.625, 460),)
        assert 0.5 not in clock.sleeps
        assert rig.frames_served == len(result.samples)  # no step-down frame


class TestHotPixels:
    """Review N1: hot pixels are never stars and never make a frame clipped."""

    def test_a_frame_of_clipped_hot_pixels_and_no_star_is_no_star_without_a_step_down(
        self,
    ) -> None:
        rig, focuser, clock, result = _run(stars=(), rig_options={"hot_pixels": 300})

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == "no_star"
        assert result.exposure_attempts == ((_START_EXPOSURE_MS, 460),)  # no step-down
        assert 0.5 not in clock.sleeps
        assert focuser.get_position() == _START

    def test_the_sweep_ignores_hot_pixels(self) -> None:
        rig, _focuser, _clock, result = _run(rig_options={"hot_pixels": 300})

        assert result.status is AutofocusStatus.SUCCESS, result
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 10
        assert result.tracked_target is not None
        assert abs(result.tracked_target[0] - _TARGET[0]) < 3.0
        assert result.target_candidates == 4


class TestCameraFullScale:
    """Review N3: the clipping test uses the camera's full scale, not a guess from the frame."""

    #: A dim scene on a 16-bit camera: one star whose top reaches ~4060 ADU at focus, no
    #: pixel above 4090 -- far below the camera's full scale (65535).
    _DIM = {"peak_at_focus": 3060.0, "background": 1000.0, "clip": 4090.0}
    _ONE_STAR = ((350.0, 256.0, 1.0),)

    def test_a_dim_star_of_a_16_bit_camera_is_not_saturated(self) -> None:
        _rig, _focuser, clock, result = _run(
            stars=self._ONE_STAR, rig_options=self._DIM, camera_bit_depth=16
        )

        assert result.status is AutofocusStatus.SUCCESS, result
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 10
        assert result.exposure_attempts == ((_START_EXPOSURE_MS, 460),)
        assert 0.5 not in clock.sleeps

    def test_without_the_bit_depth_a_dim_16_bit_frame_reads_as_12_bit(self) -> None:
        # The documented fallback hazard (frame maximum < 4096 -> full scale 4095): the dim
        # star is taken for clipped and stepped down -- what the bit depth wiring prevents.
        _rig, _focuser, _clock, result = _run(stars=self._ONE_STAR, rig_options=self._DIM)

        assert len(result.exposure_attempts) > 1

    def test_a_12_bit_camera_clipped_at_4095_is_stepped_down(self) -> None:
        twelve_bit = {"peak_at_focus": 2.0e5, "background": 100.0, "clip": 4095.0}

        rig, _focuser, _clock, result = _run(rig_options=twelve_bit, camera_bit_depth=12)

        assert result.status is AutofocusStatus.SUCCESS, result
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 10
        assert len(_assert_halvings_from_the_start(result)) >= 3
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)


class TestProductionSteps:
    """Review L2: a small, noisy star near focus at the production steps (250 / 25)."""

    _SMALL = {"sigma_at_focus": 1.5, "sigma_per_step": 0.01, "peak_at_focus": 4.0e5}

    def test_the_size_is_a_clean_v_at_the_production_fine_step(self) -> None:
        # The old centre-radius HFD jumped in steps for small stars (4.0 at both 495 and 520);
        # now every 25-step move changes it by more than the searcher's 5 % margin, and
        # repeated noisy frames at one position agree within 1 %.
        focuser = FakeFocuser()
        rig = _Rig(focuser, FakeClock(), **self._SMALL)  # type: ignore[arg-type]
        rig.exposure_ms = _START_EXPOSURE_MS / 16
        sizes = {}
        for position in (450, 475, 500, 525, 550):
            focuser.move_absolute(position)
            samples = []
            for _ in range(3):
                result = StarTargetTracker().acquire(rig.image())
                assert result.size_px is not None, result
                samples.append(result.size_px)
            assert max(samples) / min(samples) < 1.01, (position, samples)
            sizes[position] = sum(samples) / 3
        assert sizes[475] > 1.05 * sizes[500] and sizes[525] > 1.05 * sizes[500]
        assert sizes[450] > 1.05 * sizes[475] and sizes[550] > 1.05 * sizes[525]

    @pytest.mark.parametrize("start", [300, 450, 700])
    def test_a_production_step_sweep_lands_near_focus(self, start: int) -> None:
        # Within two fine steps: the frozen searcher (autofocus_search.py) does not refine
        # below its coarse probes when both neighbours are worse -- a recorded follow-up.
        _rig, _focuser, _clock, result = _run(
            start=start,
            rig_options=self._SMALL,
            coarse_step=250,
            fine_step=25,
            start_exposure_ms=_START_EXPOSURE_MS / 16,
        )

        assert result.status in (AutofocusStatus.SUCCESS, AutofocusStatus.ALREADY_FOCUSED), result
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 50
