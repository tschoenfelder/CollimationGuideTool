"""Issue #33 (artificial star): autofocus on EXACTLY ONE artificial star --
no natural star field, no ASTAP, same target through the whole sweep,
donut/saturated/missing samples rejected explicitly, fresh-frame final
validation, exposure reduced when the star saturates at focus.

The rig below is a synthetic, deterministic focus sweep: star width and
brightness are pure functions of the focuser's position (best focus at
`_BEST`), with a donut in the defocused wings -- expected optimum known
independently of the autofocus code under test."""

from __future__ import annotations

import time

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.port import FocuserMoveResult
from astrotool_core.testing.frame_factory import (
    StarSpec,
    donut_image,
    single_star_image,
    star_field_image,
)
from collimation_tool.application.autofocus_controller import (
    AutofocusController,
    AutofocusMode,
    ExposureControl,
)
from collimation_tool.application.autofocus_search import AutofocusStatus

_SHAPE = (160, 160)
_BEST = 500
_NORMAL_WITHIN = 260  # |position - best| beyond this the star is a donut
_INVISIBLE_BEYOND = 400  # ... and beyond this nothing is detected at all
_MIN_EXPOSURE_MS = 12.0


class _Rig:
    def __init__(
        self,
        focuser: FakeFocuser,
        *,
        peak_at_focus: float = 5000.0,
        exposure_ms: float = 100.0,
        distractor: bool = False,
        distractor_only_when_target_gone: bool = False,
    ) -> None:
        self.focuser = focuser
        self.peak_at_focus = peak_at_focus
        self.exposure_ms = exposure_ms
        self.gain = 100
        self.distractor = distractor
        self.distractor_only_when_gone = distractor_only_when_target_gone
        self.min_exposure_ms = _MIN_EXPOSURE_MS
        self.paused_calls: list[bool] = []

    def image(self) -> np.ndarray:
        distance = abs(self.focuser.get_position() - _BEST)
        if distance <= _NORMAL_WITHIN:
            sigma = 1.5 + 0.01 * distance
            peak = self.peak_at_focus * (self.exposure_ms / 100.0) * (1.5 / sigma) ** 2
            stars = [StarSpec(x=80.0, y=80.0, peak=peak, sigma=sigma)]
            target_visible = True
        elif distance <= _INVISIBLE_BEYOND:
            radius = 12.0 + (distance - _NORMAL_WITHIN) * 0.1
            base = donut_image(
                _SHAPE, outer_center=(80.0, 80.0), outer_radius=radius,
                inner_center=(80.0, 80.0), inner_radius=radius * 0.4,
                peak=2000.0, background=100.0,
            )
            if self.distractor and not self.distractor_only_when_gone:
                return np.asarray(base + self._stray() - 100.0)
            return np.asarray(base)
        else:
            stars = []
            target_visible = False
        if self.distractor and (not self.distractor_only_when_gone or not target_visible):
            stars.append(StarSpec(x=140.0, y=30.0, peak=9000.0, sigma=1.5))
        return star_field_image(_SHAPE, stars, background=100.0)

    @staticmethod
    def _stray() -> np.ndarray:
        return single_star_image(_SHAPE, x=140.0, y=30.0, peak=9000.0, sigma=1.5, background=100.0)

    def wait_for_frame(
        self, reference_monotonic: float, timeout_s: float
    ) -> FrameAcquisitionResult:
        return FrameAcquisitionResult(
            status=FrameAcquisitionStatus.OK,
            frame=DeliveredFrame(
                pixels=self.image(), captured_at_monotonic=time.monotonic(), exposure_seconds=0.01
            ),
        )

    def get_frame(self) -> np.ndarray:
        return self.image()

    def set_auto_exposure_paused(self, paused: bool) -> None:
        self.paused_calls.append(paused)

    def exposure_control(self) -> ExposureControl:
        def set_values(exposure_ms: float, gain: int) -> None:
            self.exposure_ms = max(exposure_ms, self.min_exposure_ms)
            self.gain = gain

        return ExposureControl(
            get=lambda: (self.exposure_ms, self.gain), set=set_values, settle_s=0.0
        )


class _RecordingFocuser(FakeFocuser):
    def __init__(self) -> None:
        super().__init__()
        self.commanded: list[int] = []

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        self.commanded.append(steps)
        return super().move_absolute(steps)


def _controller(
    focuser: FakeFocuser, rig: _Rig, *, exposure: bool = False, coarse_step: int = 100
) -> AutofocusController:
    return AutofocusController(
        focuser,
        get_frame=rig.get_frame,
        wait_for_frame=rig.wait_for_frame,
        set_auto_exposure_paused=rig.set_auto_exposure_paused,
        coarse_step=coarse_step, fine_step=10,
        exposure_control=rig.exposure_control() if exposure else None,
    )


class TestOneArtificialStarSweep:
    def test_a_one_star_sweep_converges_on_the_known_optimum(self) -> None:
        focuser = _RecordingFocuser()
        focuser.move_absolute(700)
        rig = _Rig(focuser)

        result = _controller(focuser, rig).run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.SUCCESS
        assert result.mode is AutofocusMode.ARTIFICIAL_STAR
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 10
        assert result.tracked_target is not None
        assert abs(result.tracked_target[0] - 80.0) < 2.0

    def test_donut_wings_beyond_a_coarse_step_are_invalid_not_fatal(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(520)
        rig = _Rig(focuser)

        result = _controller(focuser, rig, coarse_step=250).run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.SUCCESS
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 25
        reasons = {point.reason for point in result.samples if not point.valid}
        assert "donut_like" in reasons

    def test_a_start_that_is_already_at_focus_is_already_focused(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(_BEST)
        rig = _Rig(focuser)

        result = _controller(focuser, rig).run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.ALREADY_FOCUSED

    def test_every_commanded_position_stays_within_the_start_envelope(self) -> None:
        focuser = _RecordingFocuser()
        focuser.move_absolute(700)
        start = focuser.get_position()
        focuser.commanded.clear()
        rig = _Rig(focuser)

        _controller(focuser, rig).run(AutofocusMode.ARTIFICIAL_STAR)

        assert all(abs(position - start) <= 1000 for position in focuser.commanded)


class TestTargetIdentity:
    def test_two_candidates_at_the_start_are_reported_not_guessed(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(700)
        rig = _Rig(focuser, distractor=True)

        result = _controller(focuser, rig).run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == "multiple_candidates"

    def test_a_bright_point_that_appears_when_the_target_is_lost_is_never_measured(
        self,
    ) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(520)
        rig = _Rig(focuser, distractor=True, distractor_only_when_target_gone=True)

        result = _controller(focuser, rig, coarse_step=250).run(AutofocusMode.ARTIFICIAL_STAR)

        # Wherever the target isn't visible the sample is invalid -- the
        # stray 9000-count point is never substituted for it.
        assert result.status is AutofocusStatus.SUCCESS
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 25


class TestSaturationAtFocus:
    def test_the_exposure_is_reduced_until_the_star_no_longer_saturates_then_restored(
        self,
    ) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(700)
        rig = _Rig(focuser, peak_at_focus=90000.0)  # saturates at focus at 100 ms

        result = _controller(focuser, rig, exposure=True).run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.SUCCESS
        assert result.best_position is not None
        assert abs(result.best_position - _BEST) <= 10
        assert len(result.exposure_attempts) >= 2
        assert (rig.exposure_ms, rig.gain) == (100.0, 100)  # original restored

    def test_when_the_exposure_cannot_go_lower_the_run_says_so_and_restores(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(700)
        rig = _Rig(focuser, peak_at_focus=90000.0, exposure_ms=100.0)
        rig.min_exposure_ms = 100.0  # already at the camera's floor

        result = _controller(focuser, rig, exposure=True).run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == "saturated_at_min_exposure"
        assert (rig.exposure_ms, rig.gain) == (100.0, 100)

    def test_without_exposure_control_saturation_is_reported_plainly(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(_BEST)
        rig = _Rig(focuser, peak_at_focus=90000.0)

        result = _controller(focuser, rig).run(AutofocusMode.ARTIFICIAL_STAR)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.failure_reason == "saturated"

    def test_the_exposure_is_restored_when_the_run_is_cancelled(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(700)
        rig = _Rig(focuser, peak_at_focus=90000.0)

        result = _controller(focuser, rig, exposure=True).run(
            AutofocusMode.ARTIFICIAL_STAR, cancel_check=lambda: True
        )

        assert result.status in (AutofocusStatus.CANCELLED, AutofocusStatus.SUCCESS)
        assert (rig.exposure_ms, rig.gain) == (100.0, 100)


def test_artificial_star_autofocus_needs_no_plate_solver() -> None:
    import collimation_tool.application.autofocus_controller as module

    assert not hasattr(module, "AstapSolver")
    assert not hasattr(module, "AstapCliSolver")
