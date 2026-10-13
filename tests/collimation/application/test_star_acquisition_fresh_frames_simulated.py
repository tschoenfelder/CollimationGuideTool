"""S6.7 (#52, ROLLOUT BLOCKER from S6.0b; #39/#43/#30 class): every reacquisition measurement
after a mount motion uses a guide frame whose exposure STARTED after that motion ended plus the
settle -- on BOTH reacquisition paths (angular GOTO, S6.0b; guide pulses, S6.0d) -- under an
exposure-scaled deadline with an explicit timeout outcome.

The guide camera STREAMS on the FakeClock (back-to-back exposures, like the real ToupTek stream):
each frame shows the star where the simulated controller pointed when that exposure STARTED, so
a frame that overlapped a move shows the pre-move position (the most misleading case). The
production `OnStepMountPulseAdapter` runs on the #51 OnStep simulator; motion windows are
recorded at the simulated controller. `latest_frame` is the live view's "latest displayed frame"
(`CameraPanel.latest_mono_frame`); `wait_for_frame_after` is `CameraPanel.wait_for_frame_after`'s
contract over the same stream (a ring with a stale backlog, read non-destructively).

Before S6.7 the angular path measured `latest_frame` after a 750 ms settle (a 2 s exposure that
started before the GOTO ended was measured), and the guide-pulse path gave up after a fixed 6 s
per stage (any exposure >~2.5 s on a stream failed)."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import pytest
from astrotool_core.acquisition import DeliveredFrame, FrameAcquisitionResult, acquire_stable_frame
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, AxisResponse, CalibrationMatrix, MountAxis
from astrotool_core.mount.movement_sizing import SIDEREAL_ARCSEC_PER_S
from astrotool_core.mount.operating_mode import OperatingMode
from astrotool_core.onstep import OnStepMountParkAdapter, OnStepMountPulseAdapter
from astrotool_core.registration.geometry import polygon_centroid, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)
from astrotool_core.testing import (
    GuidePulseScenario,
    OnStepScenario,
    make_simulated_onstep_connection,
)
from astrotool_core.testing.frame_factory import single_star_image
from astrotool_core.timing import FakeClock
from collimation_tool.application.guide_pulse_reacquisition import read_tracking_for_decision
from collimation_tool.application.recenter_policy import RecenterConfig
from collimation_tool.application.star_acquisition import (
    AcquisitionResult,
    AcquisitionStatus,
    FocusedStarAcquisition,
)

_MAIN_SHAPE = (80, 100)
_GUIDE_SHAPE = (300, 400)
_GUIDE_ARCSEC_PER_PX = 6.6
_SIGN = {AxisDirection.POSITIVE: 1.0, AxisDirection.NEGATIVE: -1.0}
_SETTLE_S = RecenterConfig().settle_ms / 1000.0
_STREAM_START = 100.0
#: `star_acquisition.FRESH_FRAME_TIMEOUT` / `NO_FRESH_FRAME_SOURCE`, spelled out so this module
#: also imports on the pre-S6.7 code (failing-before evidence).
FRESH_FRAME_TIMEOUT = "fresh_frame_timeout"
NO_FRESH_FRAME_SOURCE = "no_fresh_frame_source"


def _registration() -> tuple[OpticalPrior, CrossCameraRegistrationResult]:
    prior_main = OpticalPrior(
        name="main",
        sensor_width_px=_MAIN_SHAPE[1],
        sensor_height_px=_MAIN_SHAPE[0],
        pixel_scale_arcsec=1.0,
    )
    polygon = rect_polygon(
        _MAIN_SHAPE[1] * 0.25, _MAIN_SHAPE[0] * 0.25, center=(200.0, 150.0), rotation_deg=0.0
    )
    registration = CrossCameraRegistrationResult(
        method=RegistrationMethod.ARTIFICIAL_STAR,
        status=RegistrationStatus.OK_OVERLAP,
        rotation_deg=0.0,
        scale=0.25,
        polygon_a_in_b=polygon,
        confidence=0.9,
    )
    return prior_main, registration


_TARGET = polygon_centroid(_registration()[1].polygon_a_in_b or ())


def _angular_calibration() -> CalibrationMatrix:
    unit = MountAlignmentSettings().calibration_center_rate_x * SIDEREAL_ARCSEC_PER_S / 1000.0
    px = 2000 * unit / _GUIDE_ARCSEC_PER_PX
    return CalibrationMatrix(
        responses={
            (axis, direction): AxisResponse(
                axis=axis,
                direction=direction,
                duration_ms=2000,
                dx_px=_SIGN[direction] * px if axis is MountAxis.AXIS1 else 0.0,
                dy_px=_SIGN[direction] * px if axis is MountAxis.AXIS2 else 0.0,
                px_per_ms=px / 2000,
            )
            for axis in MountAxis
            for direction in AxisDirection
        }
    )


class _StreamingSky:
    """The production adapters on the OnStep simulator and a guide camera streaming
    back-to-back exposures of `exposure_s` from `_STREAM_START` on the same FakeClock."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        start: tuple[float, float],
        exposure_s: float,
        tracking: bool,
    ) -> None:
        self.clock = FakeClock(_STREAM_START + 3 * exposure_s + 0.3 * exposure_s)
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(
                parked=False, tracking=tracking, guide_pulses=GuidePulseScenario(rate_x=1.0)
            ),
            clock=self.clock,
        )
        self.mount = OnStepMountPulseAdapter(connection)
        self.mount.connect()
        self.park = OnStepMountParkAdapter(connection)
        self.park.connect()
        self.client = made[0]
        self.ha0, self.dec0 = self.client.ha_deg, self.client.dec_deg
        self.start = start
        self.exposure_s = exposure_s
        #: (clock time, star position): the position from that time on (piecewise constant;
        #: during a motion the pre-motion position -- what an overlapping frame shows).
        self.history: list[tuple[float, tuple[float, float]]] = [(0.0, self.star())]
        #: (start, end) of every commanded motion at the simulated controller.
        self.motions: list[tuple[float, float]] = []
        #: (frame exposure start, clock time it was used) of every frame handed out AFTER the
        #: first motion -- i.e. every measurement that followed a motion.
        self.used_after_motion: list[tuple[float, float]] = []
        for name in ("move_axis_deg", "guide_pulse"):
            monkeypatch.setattr(self.client, name, self._recording(getattr(self.client, name)))

    def _recording(self, original: Callable[..., object]) -> Callable[..., object]:
        def wrapper(*args: object, **kwargs: object) -> object:
            began = self.clock.monotonic()
            try:
                return original(*args, **kwargs)
            finally:
                ended = self.clock.monotonic()
                self.motions.append((began, ended))
                self.history.append((ended, self.star()))

        return wrapper

    def star(self) -> tuple[float, float]:
        ra = (self.client.ha_deg - self.ha0) * 3600.0 / _GUIDE_ARCSEC_PER_PX
        dec = (self.client.dec_deg - self.dec0) * 3600.0 / _GUIDE_ARCSEC_PER_PX
        return (self.start[0] + ra, self.start[1] + dec)

    def _position_at(self, t: float) -> tuple[float, float]:
        position = self.history[0][1]
        for when, where in self.history:
            if when <= t:
                position = where
        # An exposure that started before a motion ended shows the pre-motion position.
        for began, ended in self.motions:
            if began <= t < ended:
                return next(p for w, p in reversed(self.history) if w <= began)
        return position

    # -- the stream -----------------------------------------------------------------------
    def _frame(self, k: int) -> DeliveredFrame:
        begin = _STREAM_START + k * self.exposure_s
        x, y = self._position_at(begin)
        pixels = single_star_image(_GUIDE_SHAPE, x=x, y=y, peak=3000.0, sigma=2.0, background=100.0)
        return DeliveredFrame(pixels, begin + self.exposure_s, self.exposure_s)

    def _latest_index(self) -> int:
        return math.floor((self.clock.monotonic() - _STREAM_START) / self.exposure_s) - 1

    def _note_use(self, frame: DeliveredFrame) -> None:
        if self.motions:
            begin = frame.captured_at_monotonic - frame.exposure_seconds
            self.used_after_motion.append((begin, self.clock.monotonic()))

    def latest_frame(self) -> np.ndarray | None:
        """`CameraPanel.latest_mono_frame`: the last frame delivered, however old."""
        index = self._latest_index()
        if index < 0:
            return None
        frame = self._frame(index)
        self._note_use(frame)
        return frame.pixels

    def wait_for_frame_after(self, reference: float, timeout_s: float) -> FrameAcquisitionResult:
        """`CameraPanel.wait_for_frame_after`: walks the ring from a 3-frame stale backlog."""
        cursor = self._latest_index() - 2

        def next_frame(remaining: float) -> DeliveredFrame | None:
            nonlocal cursor
            frame = self._frame(cursor)
            now = self.clock.monotonic()
            if frame.captured_at_monotonic > now + remaining:
                self.clock.sleep(max(0.0, remaining))
                return None
            if frame.captured_at_monotonic > now:
                self.clock.sleep(frame.captured_at_monotonic - now)
            cursor += 1
            return frame

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: True,
            reference_monotonic=reference,
            timeout_s=timeout_s,
            clock=self.clock,
        )
        if result.ok and result.frame is not None:
            self._note_use(result.frame)
        return result

    # -- assertions -----------------------------------------------------------------------
    def stale_measurements(self) -> list[tuple[float, float, float]]:
        """(frame start, use time, end of the motion before it) for every measured frame that
        started before the last motion preceding its use had ended + settled."""
        stale = []
        for begin, used_at in self.used_after_motion:
            ended = [end for _b, end in self.motions if end <= used_at]
            if ended and begin < max(ended) + _SETTLE_S - 1e-9:
                stale.append((begin, used_at, max(ended)))
        return stale

    def distance_to_target(self) -> float:
        x, y = self.star()
        return math.hypot(x - _TARGET[0], y - _TARGET[1])

    def reacquire(
        self,
        mode: OperatingMode,
        *,
        fresh_frames: bool = True,
        exposure_hint: Callable[[], float | None] | None = None,
    ) -> AcquisitionResult:
        prior_main, registration = _registration()
        acquisition = FocusedStarAcquisition(roi_size=(16, 12))
        acquisition.select(
            single_star_image(_MAIN_SHAPE, x=90.0, y=40.0, peak=3000.0, sigma=2.0, background=100.0)
        )
        return acquisition.attempt_guide_reacquisition(
            self.latest_frame,
            mount=self.mount,
            guide_calibration=_angular_calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=self.clock,
            operating_mode=mode,
            tracking_state=lambda: read_tracking_for_decision(self.park),
            wait_guide_frame_after=self.wait_for_frame_after if fresh_frames else None,
            current_operating_mode=lambda: mode,
            exposure_hint=exposure_hint,
        )


_PATHS = [
    pytest.param(OperatingMode.TERRESTRIAL, False, id="angular"),
    pytest.param(OperatingMode.ASTRONOMICAL, True, id="guide_pulse"),
]


class TestEveryMeasurementAfterAMotionIsFresh:
    @pytest.mark.parametrize(("mode", "tracking"), _PATHS)
    @pytest.mark.parametrize("exposure_s", [0.5, 2.0])
    def test_no_measured_frame_started_before_the_motion_ended_and_settled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: OperatingMode,
        tracking: bool,
        exposure_s: float,
    ) -> None:
        sky = _StreamingSky(
            monkeypatch, start=(250.0, 170.0), exposure_s=exposure_s, tracking=tracking
        )

        result = sky.reacquire(mode)

        assert sky.motions, "the mount never moved"
        assert sky.used_after_motion, "nothing was measured after a motion"
        assert sky.stale_measurements() == []
        assert result.failure_reason is None, result
        assert result.status is AcquisitionStatus.SEARCHING_GUIDE
        assert sky.distance_to_target() <= 6.0


class TestLongExposures:
    @pytest.mark.parametrize(("mode", "tracking"), _PATHS)
    @pytest.mark.parametrize("exposure_s", [4.0, 6.0])
    def test_a_long_guide_exposure_is_waited_for_not_timed_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
        mode: OperatingMode,
        tracking: bool,
        exposure_s: float,
    ) -> None:
        sky = _StreamingSky(
            monkeypatch, start=(250.0, 170.0), exposure_s=exposure_s, tracking=tracking
        )

        result = sky.reacquire(mode)

        assert result.failure_reason is None, result
        assert sky.stale_measurements() == []
        assert sky.distance_to_target() <= 6.0


class TestTheGuiExposureSnapshot:
    """S6.6b (S6.7 follow-up): the guide frame waits are scaled from the exposure the GUI
    thread snapshotted (`exposure_hint`) from the very first wait -- without it the first wait
    assumes 2.5 s and a first frame > ~5 s with no older frame to learn from ends in an explicit
    fresh_frame_timeout. (This harness always has an older frame the shared rule learns from,
    so it cannot show that timeout; the budget-from-the-start rule is
    tests/core/acquisition/test_fresh_frame_after_motion.py::TestLongExposures; here: wiring.)"""

    @pytest.mark.parametrize(("mode", "tracking"), _PATHS)
    def test_the_snapshot_reaches_the_fresh_frame_source(
        self, monkeypatch: pytest.MonkeyPatch, mode: OperatingMode, tracking: bool
    ) -> None:
        from astrotool_core.acquisition.fresh_frame_after_motion import FreshFrameSource
        from collimation_tool.application import star_acquisition

        hints: list[float | None] = []
        real = FreshFrameSource

        def spy(*args: object, **kwargs: object) -> object:
            source = real(*args, **kwargs)  # type: ignore[arg-type]
            hints.append(source._exposure())
            return source

        monkeypatch.setattr(star_acquisition, "FreshFrameSource", spy)
        sky = _StreamingSky(monkeypatch, start=(250.0, 170.0), exposure_s=8.0, tracking=tracking)

        result = sky.reacquire(mode, exposure_hint=lambda: 8.0)

        assert hints and all(hint == 8.0 for hint in hints)
        assert result.failure_reason is None, result
        assert sky.stale_measurements() == []
        assert sky.distance_to_target() <= 6.0


class TestExplicitOutcomes:
    @pytest.mark.parametrize(("mode", "tracking"), _PATHS)
    def test_without_a_fresh_frame_source_nothing_moves_and_it_says_why(
        self, monkeypatch: pytest.MonkeyPatch, mode: OperatingMode, tracking: bool
    ) -> None:
        sky = _StreamingSky(monkeypatch, start=(250.0, 170.0), exposure_s=0.5, tracking=tracking)

        result = sky.reacquire(mode, fresh_frames=False)

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == NO_FRESH_FRAME_SOURCE
        assert result.detail
        assert sky.motions == []

    @pytest.mark.parametrize(("mode", "tracking"), _PATHS)
    def test_a_stream_that_stops_after_a_move_is_an_explicit_frame_timeout(
        self, monkeypatch: pytest.MonkeyPatch, mode: OperatingMode, tracking: bool
    ) -> None:
        sky = _StreamingSky(monkeypatch, start=(250.0, 170.0), exposure_s=0.5, tracking=tracking)
        waiter = sky.wait_for_frame_after

        def dead_after_motion(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            if sky.motions:  # the camera stopped delivering after the first move
                sky.clock.sleep(timeout_s)
                return acquire_stable_frame(
                    lambda _r: None, is_available=lambda: True,
                    reference_monotonic=reference, timeout_s=0.0, clock=sky.clock,
                )
            return waiter(reference, timeout_s)

        monkeypatch.setattr(sky, "wait_for_frame_after", dead_after_motion)

        result = sky.reacquire(mode)

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == FRESH_FRAME_TIMEOUT
        assert result.detail is not None and "no frame arrived" in result.detail
        assert sky.used_after_motion == []  # nothing stale was measured instead
