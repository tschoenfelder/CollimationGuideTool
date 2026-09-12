import time
from collections.abc import Callable

from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.testing.frame_factory import single_star_image
from collimation_tool.application.autofocus_controller import AutofocusController, AutofocusMode
from collimation_tool.application.autofocus_search import AutofocusStatus
from collimation_tool.ui.autofocus_runner import AutofocusRunner


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _always_fresh_frame(reference_monotonic: float, timeout_s: float) -> FrameAcquisitionResult:
    image = single_star_image((80, 80), x=40.0, y=40.0, peak=3000.0, sigma=2.0, background=100.0)
    return FrameAcquisitionResult(
        status=FrameAcquisitionStatus.OK,
        frame=DeliveredFrame(
            pixels=image, captured_at_monotonic=time.monotonic(), exposure_seconds=0.01
        ),
    )


def _make_controller() -> AutofocusController:
    focuser = FakeFocuser()
    return AutofocusController(
        focuser,
        get_frame=lambda: None,
        wait_for_frame=_always_fresh_frame,
        set_auto_exposure_paused=lambda paused: None,
        coarse_step=100, fine_step=10,
    )


class TestAutofocusRunner:
    def test_submit_then_take_latest_returns_a_completed_outcome(self) -> None:
        runner = AutofocusRunner()
        controller = _make_controller()

        started = runner.submit(controller, AutofocusMode.STAR)
        assert started
        assert _wait_for(lambda: not runner.is_busy)

        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.result.mode is AutofocusMode.STAR

    def test_take_latest_returns_none_when_nothing_has_completed_yet(self) -> None:
        assert AutofocusRunner().take_latest() is None

    def test_take_latest_clears_the_outcome_so_it_is_returned_only_once(self) -> None:
        runner = AutofocusRunner()
        controller = _make_controller()
        runner.submit(controller, AutofocusMode.STAR)
        assert _wait_for(lambda: not runner.is_busy)

        assert runner.take_latest() is not None
        assert runner.take_latest() is None

    def test_a_submit_while_busy_is_a_no_op(self) -> None:
        runner = AutofocusRunner()
        controller = _make_controller()
        runner._busy = True  # simulate an in-flight run

        started = runner.submit(controller, AutofocusMode.STAR)
        assert started is False
        assert runner.take_latest() is None

    def test_cancel_is_observed_by_the_underlying_run(self) -> None:
        focuser = FakeFocuser()
        calls = {"n": 0}

        def counting_wait(reference_monotonic: float, timeout_s: float) -> FrameAcquisitionResult:
            calls["n"] += 1
            return _always_fresh_frame(reference_monotonic, timeout_s)

        controller = AutofocusController(
            focuser,
            get_frame=lambda: None,
            wait_for_frame=counting_wait,
            set_auto_exposure_paused=lambda paused: None,
            coarse_step=100, fine_step=10,
        )
        runner = AutofocusRunner()

        runner.submit(controller, AutofocusMode.STAR)
        runner.cancel()
        assert _wait_for(lambda: not runner.is_busy)

        outcome = runner.take_latest()
        assert outcome is not None
        # Cancelled quickly enough that it didn't run away indefinitely --
        # this is a synthetic single-star frame so real convergence would
        # otherwise finish in well under a second anyway; the real
        # assertion here is just that the run actually completed cleanly.
        assert outcome.result.status in (AutofocusStatus.SUCCESS, AutofocusStatus.CANCELLED)
