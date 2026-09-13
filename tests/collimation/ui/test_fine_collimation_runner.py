"""Tests for FineCollimationRunner — the fifth instance of this
project's established "long operation, poll for result" convention
(submit()/take_latest()/cancel(), background daemon thread), driving
FineCollimationController.run() off the UI thread."""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np
from astrotool_core.diffraction.optical_reference_model import OpticalConfig
from astrotool_core.testing.frame_factory import airy_pattern_image
from collimation_tool.application.fine_collimation_controller import FineCollimationController
from collimation_tool.application.star_acquisition import FocusedStarAcquisition
from collimation_tool.ui.fine_collimation_runner import FineCollimationRunner

_SHAPE = (140, 140)
_ROI_SIZE = (120, 120)


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _star_frame(rng: np.random.Generator) -> np.ndarray:
    dx = float(rng.uniform(-1.5, 1.5))
    dy = float(rng.uniform(-1.5, 1.5))
    frame = airy_pattern_image(
        _SHAPE, x=70.0 + dx, y=70.0 + dy, peak=5000.0, core_sigma=2.0, background=100.0,
        ring_radius_px=12.0, ring_peak=1200.0, ring_sigma=1.5,
    )
    return frame + rng.normal(0.0, 10.0, size=_SHAPE)


def _make_controller(*, sample_count: int = 4) -> FineCollimationController:
    rng = np.random.default_rng(7)
    return FineCollimationController(
        FocusedStarAcquisition(roi_size=_ROI_SIZE),
        get_frame=lambda: _star_frame(rng),
        optical_config=OpticalConfig(),
        sample_count=sample_count,
    )


class TestFineCollimationRunner:
    def test_submit_then_take_latest_returns_a_completed_outcome(self) -> None:
        runner = FineCollimationRunner()
        controller = _make_controller()

        started = runner.submit(controller)
        assert started
        assert _wait_for(lambda: not runner.is_busy)

        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.outcome.status == "success"

    def test_take_latest_returns_none_when_nothing_has_completed_yet(self) -> None:
        assert FineCollimationRunner().take_latest() is None

    def test_take_latest_clears_the_outcome_so_it_is_returned_only_once(self) -> None:
        runner = FineCollimationRunner()
        runner.submit(_make_controller())
        assert _wait_for(lambda: not runner.is_busy)

        assert runner.take_latest() is not None
        assert runner.take_latest() is None

    def test_a_submit_while_busy_is_a_no_op(self) -> None:
        runner = FineCollimationRunner()
        runner._busy = True  # simulate an in-flight run

        started = runner.submit(_make_controller())
        assert started is False
        assert runner.take_latest() is None

    def test_cancel_is_observed_by_the_underlying_run(self) -> None:
        runner = FineCollimationRunner()
        controller = _make_controller(sample_count=1000)

        runner.submit(controller)
        runner.cancel()
        assert _wait_for(lambda: not runner.is_busy)

        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.outcome.status == "failed"
        assert outcome.outcome.reason == "cancelled"
