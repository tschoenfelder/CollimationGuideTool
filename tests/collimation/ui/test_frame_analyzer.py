import time
from collections.abc import Callable

import numpy as np
from astropy.io import fits
from astrotool_core.frames.frame import Frame
from astrotool_core.frames.pixel_format import BayerPattern
from collimation_tool.application.collimation_controller import CollimationController
from collimation_tool.ui.frame_analyzer import FrameAnalyzer


def _mono_frame(value: float = 500.0, shape: tuple[int, int] = (64, 64)) -> Frame:
    return Frame(
        pixels=np.full(shape, value, dtype=np.float32), header=fits.Header(), exposure_seconds=0.1
    )


def _wait_for(predicate: Callable[[], bool], *, timeout_s: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class TestFrameAnalyzer:
    def test_submit_then_take_latest_returns_a_completed_outcome(self) -> None:
        analyzer = FrameAnalyzer(CollimationController())
        analyzer.submit(_mono_frame(), is_color=False, bayer_pattern=BayerPattern.MONO)

        assert _wait_for(lambda: not analyzer.is_busy)
        outcome = analyzer.take_latest()
        assert outcome is not None
        assert outcome.stretched.dtype == np.uint8
        assert outcome.result is not None

    def test_take_latest_returns_none_when_nothing_has_completed_yet(self) -> None:
        analyzer = FrameAnalyzer(CollimationController())
        assert analyzer.take_latest() is None

    def test_take_latest_clears_the_outcome_so_it_is_returned_only_once(self) -> None:
        analyzer = FrameAnalyzer(CollimationController())
        analyzer.submit(_mono_frame(), is_color=False, bayer_pattern=BayerPattern.MONO)
        assert _wait_for(lambda: not analyzer.is_busy)

        assert analyzer.take_latest() is not None
        assert analyzer.take_latest() is None

    def test_a_submission_while_busy_is_dropped_not_queued(self) -> None:
        analyzer = FrameAnalyzer(CollimationController())
        analyzer._busy = True  # simulate an in-flight analysis
        analyzer.submit(_mono_frame(), is_color=False, bayer_pattern=BayerPattern.MONO)
        time.sleep(0.05)
        # Nothing was started — is_busy stays True only because we forced
        # it, not because a second thread is also running.
        assert analyzer.take_latest() is None

    def test_analysis_never_blocks_the_caller(self) -> None:
        analyzer = FrameAnalyzer(CollimationController())
        # A large-ish frame, closer to a real camera's resolution, to make
        # sure submit() genuinely returns immediately rather than running
        # the pipeline inline.
        frame = _mono_frame(shape=(1080, 1920))
        start = time.monotonic()
        analyzer.submit(frame, is_color=False, bayer_pattern=BayerPattern.MONO)
        elapsed = time.monotonic() - start
        assert elapsed < 0.1


class TestColorDisplay:
    """See the real bug this was found from: "guide cam is color, but
    picture seems mono" — the live view's `stretched` output was always
    built from the mono luma plane analysis uses internally, even for a
    color camera, so a color sensor's feed never showed color at all.
    `stretched` must now be a 3-channel (H, W, 3) array for a color
    camera and stay a 2D mono array for a mono one."""

    def test_a_color_cameras_stretched_output_has_three_channels(self) -> None:
        analyzer = FrameAnalyzer(CollimationController())
        analyzer.submit(_mono_frame(shape=(8, 8)), is_color=True, bayer_pattern=BayerPattern.RGGB)
        assert _wait_for(lambda: not analyzer.is_busy)

        outcome = analyzer.take_latest()
        assert outcome is not None
        assert outcome.stretched.ndim == 3
        assert outcome.stretched.shape[2] == 3
        assert outcome.stretched.dtype == np.uint8

    def test_a_mono_cameras_stretched_output_stays_two_dimensional(self) -> None:
        analyzer = FrameAnalyzer(CollimationController())
        analyzer.submit(_mono_frame(shape=(8, 8)), is_color=False, bayer_pattern=BayerPattern.MONO)
        assert _wait_for(lambda: not analyzer.is_busy)

        outcome = analyzer.take_latest()
        assert outcome is not None
        assert outcome.stretched.ndim == 2
