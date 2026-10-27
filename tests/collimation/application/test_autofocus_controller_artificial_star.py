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
    ) -> None:
        self.focuser = focuser
        self.clock = clock
        self.stars = stars
        self.exposure_ms = _START_EXPOSURE_MS
        self.gain = 460
        self.min_exposure_ms = 0.1
        self.frames_served = 0
        self._rng = np.random.default_rng(58)

    def sigma(self) -> float:
        return _SIGMA_AT_FOCUS + 0.016 * abs(self.focuser.get_position() - _BEST)

    def peak(self, brightness: float = 1.0) -> float:
        """The star's true peak above the background (before clipping)."""
        scale = (self.exposure_ms / _START_EXPOSURE_MS) * (_SIGMA_AT_FOCUS / self.sigma()) ** 2
        return _PEAK_AT_FOCUS * brightness * scale

    def image(self) -> np.ndarray:
        sigma = self.sigma()
        stars = [StarSpec(x=x, y=y, peak=self.peak(b), sigma=sigma) for x, y, b in self.stars]
        image = star_field_image(_SHAPE, stars, background=_BACKGROUND).astype(np.float64)
        image += self._rng.normal(0.0, 30.0, _SHAPE)
        return np.clip(np.round(image), 0.0, _FULL_SCALE)

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
    return AutofocusController(
        focuser,
        get_frame=rig.image,
        wait_for_frame=rig.wait_for_frame,
        set_auto_exposure_paused=lambda paused: None,
        coarse_step=100,
        fine_step=10,
        exposure_control=rig.exposure_control(),
        clock=clock,
        **kwargs,  # type: ignore[arg-type]
    )


def _run(
    *,
    start: int = _START,
    min_exposure_ms: float = 0.1,
    stars: tuple[tuple[float, float, float], ...] = _FOUR_STARS,
    **controller_kwargs: object,
) -> tuple[_Rig, FakeFocuser, FakeClock, AutofocusResult]:
    clock = FakeClock()
    focuser = FakeFocuser()
    focuser.move_absolute(start)
    rig = _Rig(focuser, clock, stars)
    rig.min_exposure_ms = min_exposure_ms
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
