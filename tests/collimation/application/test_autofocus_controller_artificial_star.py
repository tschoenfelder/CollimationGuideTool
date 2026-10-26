"""Issue #58: artificial-star autofocus with SEVERAL heavily over-exposed stars (diagnostic
4c5677d0: four clipped blobs at 2000 ms, `no_usable_evidence` / `multiple_candidates` on the
first frame, the focuser never moved).

Simulated sweep on a FakeClock: four broad stars clipped at 65520 at the start exposure. Star
width and brightness are pure functions of the focuser position (best focus at `_BEST`) and of
the exposure (peak proportional to it) -- the expected optimum and the star to follow (the one
nearest the image centre, NOT the brightest) are known independently of the code under test.
`detect_sources` fragments the blobs at the start (> 25000 bright pixels) and, on the way down,
skips each still-large blob (component > 5000 px) -- both are the library's real behaviour."""

from __future__ import annotations

import threading

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.fake_focuser import FakeFocuser
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

_SHAPE = (240, 320)  # (height, width): centre (159.5, 119.5)
_BEST = 500
_START = 700
_START_EXPOSURE_MS = 2000.0
_FULL_SCALE = 65520.0
#: Peak (ADU above background) of the target at best focus and the start exposure.
_PEAK_AT_FOCUS = 4.0e6
_SIGMA_AT_FOCUS = 8.0
#: The target: nearest the centre but the FAINTEST of the four.
_TARGET = (175.0, 128.0)
_OTHERS = ((60.0, 55.0, 1.5), (265.0, 60.0, 1.3), (250.0, 185.0, 1.2))  # x, y, brightness


class _Rig:
    def __init__(self, focuser: FakeFocuser, clock: FakeClock) -> None:
        self.focuser = focuser
        self.clock = clock
        self.exposure_ms = _START_EXPOSURE_MS
        self.gain = 460
        self.min_exposure_ms = 0.1
        self.frames_served = 0
        self._rng = np.random.default_rng(58)

    def sigma(self) -> float:
        return _SIGMA_AT_FOCUS + 0.016 * abs(self.focuser.get_position() - _BEST)

    def image(self) -> np.ndarray:
        sigma = self.sigma()
        scale = (self.exposure_ms / _START_EXPOSURE_MS) * (_SIGMA_AT_FOCUS / sigma) ** 2
        stars = [StarSpec(x=_TARGET[0], y=_TARGET[1], peak=_PEAK_AT_FOCUS * scale, sigma=sigma)]
        stars += [
            StarSpec(x=x, y=y, peak=_PEAK_AT_FOCUS * brightness * scale, sigma=sigma)
            for x, y, brightness in _OTHERS
        ]
        image = star_field_image(_SHAPE, stars, background=1000.0).astype(np.float64)
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


def _run(
    *, min_exposure_ms: float = 0.1, **controller_kwargs: object
) -> tuple[_Rig, FakeFocuser, FakeClock, AutofocusResult]:
    clock = FakeClock()
    focuser = FakeFocuser()
    focuser.move_absolute(_START)
    rig = _Rig(focuser, clock)
    rig.min_exposure_ms = min_exposure_ms
    controller = AutofocusController(
        focuser,
        get_frame=rig.image,
        wait_for_frame=rig.wait_for_frame,
        set_auto_exposure_paused=lambda paused: None,
        coarse_step=100,
        fine_step=10,
        exposure_control=rig.exposure_control(),
        clock=clock,
        **controller_kwargs,  # type: ignore[arg-type]
    )
    result = controller.run(AutofocusMode.ARTIFICIAL_STAR)
    return rig, focuser, clock, result


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
        # The exposure was stepped down by halving from the start until the star was
        # unclipped (more than the 2 halvings max_exposure_attempts=3 allows) ...
        exposures = [exposure for exposure, _gain in result.exposure_attempts]
        assert exposures[0] == _START_EXPOSURE_MS
        assert len(exposures) >= 4
        assert all(b == a / 2 for a, b in zip(exposures, exposures[1:], strict=False))
        # ... each lowering waited its settle time on the injected clock ...
        assert clock.sleeps.count(0.5) == len(exposures) - 1
        # ... and the original exposure/gain is back afterwards.
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)

    # In this rig the target is still clipped at 125 and 62.5 ms and unclipped from 31.25 ms
    # on; between 1000 and 250 ms detect_sources finds no source at all (see module doc).
    _CLIPPED_DOWN_TO = [2000.0, 1000.0, 500.0, 250.0, 125.0, 62.5]

    def test_the_camera_minimum_exposure_ends_it_as_saturated_at_min_exposure(self) -> None:
        rig, focuser, _clock, result = _run(min_exposure_ms=62.5)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == SATURATED_AT_MIN_EXPOSURE
        assert [e for e, _g in result.exposure_attempts] == self._CLIPPED_DOWN_TO
        assert focuser.get_position() == _START  # never searched on a clipped star
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)

    def test_the_step_cap_ends_it_as_saturated_at_min_exposure(self) -> None:
        rig, focuser, _clock, result = _run(max_acquisition_exposure_steps=5)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == SATURATED_AT_MIN_EXPOSURE
        assert [e for e, _g in result.exposure_attempts] == self._CLIPPED_DOWN_TO
        assert focuser.get_position() == _START
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)

    def test_a_target_not_found_when_the_steps_run_out_is_reported_as_such(self) -> None:
        # Stopped at 500 ms, where the detector found nothing: never called "saturated".
        rig, focuser, _clock, result = _run(max_acquisition_exposure_steps=2)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == "not_found_at_tracked_position"
        assert [e for e, _g in result.exposure_attempts] == [2000.0, 1000.0, 500.0]
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
        controller = AutofocusController(
            focuser,
            get_frame=rig.image,
            wait_for_frame=rig.wait_for_frame,
            set_auto_exposure_paused=lambda paused: None,
            coarse_step=100,
            fine_step=10,
            exposure_control=rig.exposure_control(),
            clock=clock,
        )

        result = controller.run(AutofocusMode.ARTIFICIAL_STAR, cancel_event=stop)

        assert result.status is AutofocusStatus.CANCELLED
        assert focuser.get_position() == _START
        assert (rig.exposure_ms, rig.gain) == (_START_EXPOSURE_MS, 460)


class TestAWellExposedStarIsUnchanged:
    def test_no_exposure_change_and_no_extra_frame_when_the_star_is_not_clipped(self) -> None:
        clock = FakeClock()
        focuser = FakeFocuser()
        focuser.move_absolute(_START)
        rig = _Rig(focuser, clock)
        rig.exposure_ms = 15.625  # 2000 / 128: unclipped everywhere in the sweep
        controller = AutofocusController(
            focuser,
            get_frame=rig.image,
            wait_for_frame=rig.wait_for_frame,
            set_auto_exposure_paused=lambda paused: None,
            coarse_step=100,
            fine_step=10,
            exposure_control=rig.exposure_control(),
            clock=clock,
        )

        result = controller.run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.SUCCESS
        assert result.exposure_attempts == ((15.625, 460),)
        assert 0.5 not in clock.sleeps
        assert rig.frames_served == len(result.samples)  # no step-down frame
