import time
from collections.abc import Callable

import numpy as np
import pytest
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.frame_factory import single_star_image
from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner

_SHAPE = (120, 120)


def _star(x: float, y: float) -> np.ndarray:
    return single_star_image(_SHAPE, x=x, y=y, peak=2000.0, sigma=2.5, background=100.0)


def _frame_pair(
    before_xy: tuple[float, float], after_xy: tuple[float, float]
) -> Callable[[], np.ndarray]:
    frames = iter([_star(*before_xy), _star(*after_xy)])
    return lambda: next(frames)


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestMountTestMoveRunner:
    def test_submit_then_take_latest_measures_both_cameras_around_one_pulse(self) -> None:
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()

        started = runner.submit(
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            500,
            _frame_pair((50.0, 50.0), (60.0, 50.0)),
            _frame_pair((20.0, 30.0), (20.0, 45.0)),
        )

        assert started is True
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.error is None
        assert outcome.responses["left"].dx_px == pytest.approx(10.0, abs=0.5)
        assert outcome.responses["left"].dy_px == pytest.approx(0.0, abs=0.5)
        assert outcome.responses["right"].dx_px == pytest.approx(0.0, abs=0.5)
        assert outcome.responses["right"].dy_px == pytest.approx(15.0, abs=0.5)
        assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)]

    def test_take_latest_returns_none_when_nothing_has_completed_yet(self) -> None:
        assert MountTestMoveRunner().take_latest() is None

    def test_take_latest_clears_the_outcome_so_it_is_returned_only_once(self) -> None:
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()
        runner.submit(
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            500,
            _frame_pair((0.0, 0.0), (0.0, 0.0)),
            _frame_pair((0.0, 0.0), (0.0, 0.0)),
        )
        assert _wait_for(lambda: not runner.is_busy)
        assert runner.take_latest() is not None
        assert runner.take_latest() is None

    def test_a_submit_while_busy_is_a_no_op(self) -> None:
        runner = MountTestMoveRunner()
        runner._busy = True  # noqa: SLF001 -- simulate an in-flight test move
        mount = FakeMountAdapter()
        mount.connect()
        started = runner.submit(
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            500,
            lambda: _star(0.0, 0.0),
            lambda: _star(0.0, 0.0),
        )
        assert started is False

    def test_rejected_pulse_reports_an_error_and_no_responses(self) -> None:
        mount = FakeMountAdapter()  # never connected -> pulse_axis always rejects
        runner = MountTestMoveRunner()
        runner.submit(
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            500,
            lambda: _star(0.0, 0.0),
            lambda: _star(0.0, 0.0),
        )
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.error is not None
        assert outcome.responses == {}

    def test_no_frame_captured_yet_reports_an_error(self) -> None:
        mount = FakeMountAdapter()
        mount.connect()
        runner = MountTestMoveRunner()
        runner.submit(
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            500,
            lambda: None,
            lambda: _star(0.0, 0.0),
        )
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.error is not None
        assert "no frame" in outcome.error

    def test_no_star_detected_reports_an_error(self) -> None:
        mount = FakeMountAdapter()
        mount.connect()
        blank = np.full(_SHAPE, 100.0, dtype=np.float64)
        runner = MountTestMoveRunner()
        runner.submit(
            mount,
            MountAxis.AXIS1,
            AxisDirection.POSITIVE,
            500,
            lambda: blank,
            lambda: _star(0.0, 0.0),
        )
        assert _wait_for(lambda: not runner.is_busy)
        outcome = runner.take_latest()
        assert outcome is not None
        assert outcome.error is not None
        assert "no point source" in outcome.error
