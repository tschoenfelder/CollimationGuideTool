"""FrameAnalyzer — runs the collimation measurement + live-view stretch
pipeline on a background thread, off the UI thread.

Why: measured against a real captured frame (ATR585M, 3840x2160) —
building the analysis plane, measure_and_advise, and the live-view
percentile stretch together took ~900ms. Two CameraPanels each doing
that synchronously inside the poll-timer callback (the UI/main thread)
meant Qt's event loop fell far behind its own 100ms tick and the whole
window stopped responding to clicks — this class exists so that work
never touches the UI thread. `CameraPanel` submits the latest frame each
poll and picks up whatever the most recently *completed* analysis is;
both are non-blocking, so the poll tick itself stays cheap regardless of
how long analysis actually takes.

Also does color-sensor demosaicing before building the analysis plane —
a color camera's raw frame is a Bayer mosaic, not a valid mono plane on
its own (see `astrotool_core.frames.pixel_format`). The demosaiced RGB
is used two ways: `rgb_to_luma()` of it feeds the mono analysis plane
(donut/star detection cares about spatial intensity, not color), while
the full RGB itself is what gets percentile-stretched for display (see
`stretch_rgb_to_uint8`) — a mono camera has no RGB to speak of, so its
frame is stretched directly instead.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np
from astrotool_core.frames import BayerPattern, build_analysis_plane, demosaic, rgb_to_luma
from astrotool_core.frames.frame import Frame

from collimation_tool.application.collimation_controller import CollimationController
from collimation_tool.domain.collimation_measurement import DonutAnalysisResult
from collimation_tool.domain.collimation_state import CollimationRecommendation
from collimation_tool.ui.live_view import stretch_rgb_to_uint8, stretch_to_uint8


@dataclass(frozen=True)
class AnalysisOutcome:
    frame: Frame
    stretched: np.ndarray
    result: DonutAnalysisResult
    recommendation: CollimationRecommendation | None


class FrameAnalyzer:
    """Runs at most one frame's analysis at a time, on its own thread.

    ``submit()`` silently drops the frame if a previous analysis is still
    running: measurement doesn't need every single frame, just to not
    fall further and further behind. The next poll's frame will be
    fresher anyway.
    """

    def __init__(self, controller: CollimationController) -> None:
        self._controller = controller
        self._lock = threading.Lock()
        self._busy = False
        self._latest_outcome: AnalysisOutcome | None = None

    def submit(self, frame: Frame, *, is_color: bool, bayer_pattern: BayerPattern) -> None:
        with self._lock:
            if self._busy:
                return
            self._busy = True
        threading.Thread(
            target=self._run,
            args=(frame, is_color, bayer_pattern),
            daemon=True,
            name="frame-analyzer",
        ).start()

    def _run(self, frame: Frame, is_color: bool, bayer_pattern: BayerPattern) -> None:
        try:
            rgb = demosaic(frame.pixels, bayer_pattern) if is_color else None
            mono_override = rgb_to_luma(rgb) if rgb is not None else None
            plane = build_analysis_plane(frame, plane=mono_override)
            result, recommendation = self._controller.measure_and_advise(plane)
            # Display a color camera's actual color, not the mono luma
            # plane analysis uses internally — see stretch_rgb_to_uint8's
            # docstring for the bug this fixes ("guide cam is color, but
            # picture seems mono": the live view was always built from
            # the analysis-only mono plane, so a color sensor's feed
            # never showed color at all).
            stretched = (
                stretch_rgb_to_uint8(rgb) if rgb is not None else stretch_to_uint8(plane.mono)
            )
            outcome = AnalysisOutcome(
                frame=frame, stretched=stretched, result=result, recommendation=recommendation
            )
            with self._lock:
                self._latest_outcome = outcome
        finally:
            with self._lock:
                self._busy = False

    def take_latest(self) -> AnalysisOutcome | None:
        """Return and clear the latest ready outcome, if any. Cheap, non-blocking."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy
