import time
from collections.abc import Callable

import numpy as np
from astropy.io import fits
from astrotool_core.frames.frame import Frame
from astrotool_core.frames.pixel_format import BayerPattern
from collimation_tool.application.collimation_controller import CollimationController
from collimation_tool.ui.frame_analyzer import FrameAnalyzer, _demosaiced_mono


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


class TestDemosaicedMono:
    def test_mono_pattern_is_a_passthrough(self) -> None:
        plane = np.full((8, 8), 123.0, dtype=np.float32)
        result = _demosaiced_mono(plane, BayerPattern.MONO)
        assert result.shape == (8, 8)
        assert np.allclose(result, 123.0)

    def test_uniform_bayer_plane_demosaics_to_a_uniform_mono_plane(self) -> None:
        # A flat-field mosaic (every raw pixel the same value) must
        # demosaic to a flat luma plane of the same value, regardless of
        # Bayer pattern — a good sanity check independent of geometry.
        plane = np.full((8, 8), 1000.0, dtype=np.float32)
        for pattern in (BayerPattern.RGGB, BayerPattern.BGGR, BayerPattern.GRBG, BayerPattern.GBRG):
            result = _demosaiced_mono(plane, pattern)
            assert result.shape == (8, 8)
            assert np.allclose(result, 1000.0, atol=1.0), pattern
