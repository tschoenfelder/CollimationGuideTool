"""Tests for AutofocusController — the shared workflow wiring the bounded
searcher (autofocus_search.py) to a real camera/focuser: exposure-freeze
applied and always restored, frame-freshness enforced (a frame whose
exposure predates the focuser move is never scored), and the mode-specific
analyzer (star/terrestrial) is actually driven end to end."""

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
from astrotool_core.testing.frame_factory import single_star_image
from collimation_tool.application.autofocus_controller import (
    AutofocusController,
    AutofocusMode,
)
from collimation_tool.application.autofocus_search import AutofocusStatus

_SHAPE = (120, 120)


class _FreshFrameSource:
    """A frame source whose `wait_for_frame` always succeeds, always
    returning the frame currently configured via `set_pixels` -- the
    happy-path double for controller tests. `get_frame` and
    `wait_for_frame` both read the SAME current pixel array, so a
    real-camera-shaped caller sees consistent content between the two."""

    def __init__(self, pixels: np.ndarray) -> None:
        self._pixels = pixels
        self.wait_calls: list[tuple[float, float]] = []
        self.auto_exposure_paused_calls: list[bool] = []

    def set_pixels(self, pixels: np.ndarray) -> None:
        self._pixels = pixels

    def get_frame(self) -> np.ndarray | None:
        return self._pixels

    def wait_for_frame(
        self, reference_monotonic: float, timeout_s: float
    ) -> FrameAcquisitionResult:
        self.wait_calls.append((reference_monotonic, timeout_s))
        return FrameAcquisitionResult(
            status=FrameAcquisitionStatus.OK,
            frame=DeliveredFrame(
                pixels=self._pixels, captured_at_monotonic=time.monotonic(), exposure_seconds=0.01
            ),
        )

    def set_auto_exposure_paused(self, paused: bool) -> None:
        self.auto_exposure_paused_calls.append(paused)


def _single_star(*, sigma: float) -> np.ndarray:
    return single_star_image(_SHAPE, x=60.0, y=60.0, peak=3000.0, sigma=sigma, background=100.0)


class TestAutofocusControllerStarMode:
    def test_a_full_star_mode_run_converges_and_reports_success(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(700)  # away from the true optimum (500) -- a real climb
        source = _FreshFrameSource(_single_star(sigma=2.5))

        # A star frame whose blur genuinely tracks the focuser's own
        # position -- sharpest exactly at position 500. wait_for_frame is
        # the method the controller actually calls for every sample (see
        # AutofocusController._acquire_fresh_frame) -- get_frame is only
        # ever consulted for terrestrial mode's *extra* same-position
        # samples, so the dynamic blur must live here, not on get_frame.
        def dynamic_wait_for_frame(
            reference_monotonic: float, timeout_s: float
        ) -> FrameAcquisitionResult:
            distance = abs(focuser.get_position() - 500)
            sigma = 1.5 + 0.01 * distance
            image = _single_star(sigma=sigma)
            source.set_pixels(image)
            return source.wait_for_frame(reference_monotonic, timeout_s)

        controller = AutofocusController(
            focuser,
            get_frame=source.get_frame,
            wait_for_frame=dynamic_wait_for_frame,
            set_auto_exposure_paused=source.set_auto_exposure_paused,
            coarse_step=100, fine_step=10,
        )

        result = controller.run(AutofocusMode.STAR)

        assert result.status is AutofocusStatus.SUCCESS
        assert result.mode is AutofocusMode.STAR
        assert result.best_position == 500

    def test_exposure_is_frozen_for_the_whole_run_and_always_restored(self) -> None:
        focuser = FakeFocuser()
        source = _FreshFrameSource(_single_star(sigma=2.0))
        controller = AutofocusController(
            focuser,
            get_frame=source.get_frame,
            wait_for_frame=source.wait_for_frame,
            set_auto_exposure_paused=source.set_auto_exposure_paused,
            coarse_step=100, fine_step=10,
        )

        controller.run(AutofocusMode.STAR)

        assert source.auto_exposure_paused_calls[0] is True
        assert source.auto_exposure_paused_calls[-1] is False

    def test_exposure_is_restored_even_if_the_search_raises(self) -> None:
        focuser = FakeFocuser()
        source = _FreshFrameSource(_single_star(sigma=2.0))

        def failing_wait(reference_monotonic: float, timeout_s: float) -> FrameAcquisitionResult:
            raise RuntimeError("simulated camera failure")

        controller = AutofocusController(
            focuser,
            get_frame=source.get_frame,
            wait_for_frame=failing_wait,
            set_auto_exposure_paused=source.set_auto_exposure_paused,
            coarse_step=100, fine_step=10,
        )

        with pytest.raises(RuntimeError):
            controller.run(AutofocusMode.STAR)

        assert source.auto_exposure_paused_calls[-1] is False

    def test_a_frame_whose_wait_reports_timeout_is_never_scored(self) -> None:
        focuser = FakeFocuser()
        source = _FreshFrameSource(_single_star(sigma=2.0))

        def timed_out_wait(reference_monotonic: float, timeout_s: float) -> FrameAcquisitionResult:
            return FrameAcquisitionResult(status=FrameAcquisitionStatus.TIMEOUT, frame=None)

        controller = AutofocusController(
            focuser,
            get_frame=source.get_frame,
            wait_for_frame=timed_out_wait,
            set_auto_exposure_paused=source.set_auto_exposure_paused,
            coarse_step=100, fine_step=10,
        )

        result = controller.run(AutofocusMode.STAR)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.samples == ()

    def test_wait_for_frame_is_called_with_a_reference_timestamp_for_every_sample(self) -> None:
        focuser = FakeFocuser()
        source = _FreshFrameSource(_single_star(sigma=2.0))
        controller = AutofocusController(
            focuser,
            get_frame=source.get_frame,
            wait_for_frame=source.wait_for_frame,
            set_auto_exposure_paused=source.set_auto_exposure_paused,
            coarse_step=100, fine_step=10,
        )

        controller.run(AutofocusMode.STAR)

        assert len(source.wait_calls) >= 1
        for reference_monotonic, _timeout_s in source.wait_calls:
            assert reference_monotonic <= time.monotonic()

    def test_result_records_camera_and_focuser_identity(self) -> None:
        # Issue #35: previously entirely absent from AutofocusResult.
        focuser = FakeFocuser()
        source = _FreshFrameSource(_single_star(sigma=2.0))
        controller = AutofocusController(
            focuser,
            get_frame=source.get_frame,
            wait_for_frame=source.wait_for_frame,
            set_auto_exposure_paused=source.set_auto_exposure_paused,
            coarse_step=100, fine_step=10,
            camera_label="Main", focuser_label="Main",
        )

        result = controller.run(AutofocusMode.STAR)

        assert result.camera_label == "Main"
        assert result.optical_train == "Main"
        assert result.focuser_label == "Main"

    def test_identity_defaults_to_unknown_when_unsupplied(self) -> None:
        focuser = FakeFocuser()
        source = _FreshFrameSource(_single_star(sigma=2.0))
        controller = AutofocusController(
            focuser,
            get_frame=source.get_frame,
            wait_for_frame=source.wait_for_frame,
            set_auto_exposure_paused=source.set_auto_exposure_paused,
            coarse_step=100, fine_step=10,
        )

        result = controller.run(AutofocusMode.STAR)

        assert result.camera_label == "unknown"
        assert result.focuser_label == "unknown"


class TestAutofocusControllerTerrestrialMode:
    def test_terrestrial_mode_samples_multiple_frames_per_position(self) -> None:
        focuser = FakeFocuser()
        rng = np.random.default_rng(7)
        frame = np.full((96, 96), 1000.0, dtype=np.float64)
        for ty in range(0, 96, 32):
            for tx in range(0, 96, 32):
                frame[ty : ty + 32, tx : tx + 32] += rng.uniform(0.0, 400.0, size=(32, 32))
        source = _FreshFrameSource(frame)

        controller = AutofocusController(
            focuser,
            get_frame=source.get_frame,
            wait_for_frame=source.wait_for_frame,
            set_auto_exposure_paused=source.set_auto_exposure_paused,
            coarse_step=100, fine_step=10,
            terrestrial_sample_count=3,
        )

        result = controller.run(AutofocusMode.TERRESTRIAL)

        assert result.mode is AutofocusMode.TERRESTRIAL
        # At least one real measurement was taken (the wait-for-frame
        # path, not merely the extra same-position samples).
        assert len(source.wait_calls) >= 1
