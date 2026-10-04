"""S6.0d (#39): astronomical-mode guide-assisted reacquisition with OnStepAdapter 0.5.0 guide
pulses, end to end against the PRODUCTION adapter on the #51 simulator.

`FocusedStarAcquisition.attempt_guide_reacquisition` -> motion decision (operating mode, fresh
tracking reading from the production park adapter, reported capability) -> guide-pulse
calibration + `CollimationRecenterPolicy` -> the real `OnStepMountPulseAdapter` -> the simulated
controller with 0.5.0's guide-pulse model (opt-in; without it the simulator is 0.4.1). Guide
frames are rendered from where the simulated controller REALLY points, through a camera model
with rotation / mirroring / unequal axis scales, and are delivered with explicit exposure
timestamps through the production settled-frame wait -- so the star only comes back if the right
guide pulses were sent, and only fresh frames can be measured.
"""

from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable

import numpy as np
import pytest
from astrotool_core.acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    acquire_stable_frame,
)
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
    install_onstep_adapter_050_exports,
    make_simulated_onstep_connection,
)
from astrotool_core.testing.frame_factory import single_star_image
from astrotool_core.testing.sim_onstep import SimulatedOnStepIndiClient
from astrotool_core.timing import FakeClock
from collimation_tool.application.guide_pulse_reacquisition import (
    ASTRONOMICAL_NEEDS_GUIDE_PULSES,
    TRACKING_STATE_UNKNOWN,
    read_tracking_for_decision,
)
from collimation_tool.application.star_acquisition import (
    ASTRONOMICAL_REACQUISITION_UNAVAILABLE,
    MOUNT_TRACKING_STOPPED,
    OPERATING_MODE_CHANGED,
    AcquisitionResult,
    AcquisitionStatus,
    FocusedStarAcquisition,
)

_MAIN_SHAPE = (80, 100)  # height, width
_GUIDE_SHAPE = (300, 400)
_GUIDE_ARCSEC_PER_PX = 6.6
_EXPOSURE_S = 0.5
_SIGN = {AxisDirection.POSITIVE: 1.0, AxisDirection.NEGATIVE: -1.0}


def _until(condition: Callable[[], bool], *, timeout_s: float = 5.0) -> bool:
    """Barrier on another thread's progress; a real-time safety bound, never a policy wait."""
    tick = threading.Event()
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() >= deadline:
            return False
        tick.wait(0.001)
    return True


def _registration() -> tuple[OpticalPrior, CrossCameraRegistrationResult]:
    prior_main = OpticalPrior(
        name="main",
        sensor_width_px=_MAIN_SHAPE[1],
        sensor_height_px=_MAIN_SHAPE[0],
        pixel_scale_arcsec=1.0,
    )
    scale = 0.25
    polygon = rect_polygon(
        _MAIN_SHAPE[1] * scale, _MAIN_SHAPE[0] * scale, center=(200.0, 150.0), rotation_deg=0.0
    )
    registration = CrossCameraRegistrationResult(
        method=RegistrationMethod.ARTIFICIAL_STAR,
        status=RegistrationStatus.OK_OVERLAP,
        rotation_deg=0.0,
        scale=scale,
        polygon_a_in_b=polygon,
        confidence=0.9,
    )
    return prior_main, registration


_TARGET = polygon_centroid(_registration()[1].polygon_a_in_b or ())


class _Sky:
    """Production pulse + park adapters on one simulated controller; a guide camera whose
    star follows where the controller really points (x right, y down)."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        start: tuple[float, float],
        guide_api: bool = True,
        tracking: bool = True,
        guide: GuidePulseScenario | None = None,
        rot_deg: float = 0.0,
        ra_sign: float = 1.0,
        dec_sign: float = 1.0,
        ra_factor: float = 1.0,
    ) -> None:
        if guide_api:
            install_onstep_adapter_050_exports(monkeypatch)
        self.guide = guide or GuidePulseScenario(rate_x=1.0)
        self.clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(
                parked=False,
                tracking=tracking,
                guide_pulses=self.guide if guide_api else None,
            ),
            clock=self.clock,
        )
        self.mount = OnStepMountPulseAdapter(connection)
        self.mount.connect()
        self.park = OnStepMountParkAdapter(connection)
        self.park.connect()
        self.client: SimulatedOnStepIndiClient = made[0]
        self.ha0, self.dec0 = self.client.ha_deg, self.client.dec_deg
        self.start = start
        self.rot = math.radians(rot_deg)
        self.ra_sign, self.dec_sign, self.ra_factor = ra_sign, dec_sign, ra_factor
        #: Exposure start of every frame the settled-frame wait accepted, in order.
        self.frame_starts: list[float] = []
        #: (start, end) clock time of each guide pulse.
        self.pulses: list[tuple[float, float]] = []
        original = self.client.guide_pulse

        def recording(direction: str, duration_ms: int, **kwargs: float) -> object:
            started = self.clock.monotonic()
            result = original(direction, duration_ms, **kwargs)
            self.pulses.append((started, self.clock.monotonic()))
            return result

        monkeypatch.setattr(self.client, "guide_pulse", recording)

    def star(self) -> tuple[float, float]:
        ra_px = (
            self.ra_sign
            * self.ra_factor
            * (self.client.ha_deg - self.ha0)
            * 3600.0
            / _GUIDE_ARCSEC_PER_PX
        )
        dec_px = self.dec_sign * (self.client.dec_deg - self.dec0) * 3600.0 / _GUIDE_ARCSEC_PER_PX
        cos, sin = math.cos(self.rot), math.sin(self.rot)
        return (
            self.start[0] + cos * ra_px - sin * dec_px,
            self.start[1] + sin * ra_px + cos * dec_px,
        )

    def render(self) -> np.ndarray:
        x, y = self.star()
        return single_star_image(_GUIDE_SHAPE, x=x, y=y, peak=3000.0, sigma=2.0, background=100.0)

    def wait_for_frame_after(self, reference: float, timeout_s: float) -> FrameAcquisitionResult:
        """`CameraPanel.wait_for_frame_after`'s contract over a camera exposing on the clock."""

        def next_frame(_remaining: float) -> DeliveredFrame:
            self.clock.sleep(_EXPOSURE_S)
            return DeliveredFrame(self.render(), self.clock.monotonic(), _EXPOSURE_S)

        result = acquire_stable_frame(
            next_frame,
            is_available=lambda: True,
            reference_monotonic=reference,
            timeout_s=timeout_s,
            clock=self.clock,
        )
        if result.ok and result.frame is not None:
            self.frame_starts.append(
                result.frame.captured_at_monotonic - result.frame.exposure_seconds
            )
        return result

    def tracking_state(self) -> bool | None:
        return read_tracking_for_decision(self.park)

    @staticmethod
    def angular_calibration() -> CalibrationMatrix:
        """Mount Align's matrix (S6.0 unit) for the angular path, identity camera."""
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

    def reacquire(
        self,
        mode: OperatingMode = OperatingMode.ASTRONOMICAL,
        *,
        tracking_state: Callable[[], bool | None] | None = None,
        fresh_frames: bool = True,
        guide_max_position_error_px: float = 80.0,
        current_mode: Callable[[], OperatingMode] | None = None,
    ) -> AcquisitionResult:
        prior_main, registration = _registration()
        acquisition = FocusedStarAcquisition(
            roi_size=(16, 12), guide_max_position_error_px=guide_max_position_error_px
        )
        # Star near Main's right edge (90, 40): predicted in Guide at (210, 150).
        acquisition.select(
            single_star_image(_MAIN_SHAPE, x=90.0, y=40.0, peak=3000.0, sigma=2.0, background=100.0)
        )
        return acquisition.attempt_guide_reacquisition(
            self.render,
            mount=self.mount,
            guide_calibration=self.angular_calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=self.clock,
            operating_mode=mode,
            tracking_state=tracking_state or self.tracking_state,
            wait_guide_frame_after=self.wait_for_frame_after if fresh_frames else None,
            current_operating_mode=current_mode,
        )

    def distance_to_target(self) -> float:
        x, y = self.star()
        return math.hypot(x - _TARGET[0], y - _TARGET[1])

    def join_stop_worker(self) -> None:
        worker = self.mount._stop_worker
        if worker is not None:
            worker.join(timeout=5.0)
            assert not worker.is_alive(), "the stop worker never finished"


class TestAstronomicalReacquisitionWithGuidePulses:
    def test_guide_pulses_bring_the_star_back_with_tracking_kept_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sky = _Sky(monkeypatch, start=(250.0, 170.0))

        result = sky.reacquire()

        assert result.failure_reason is None, result
        assert result.status is AcquisitionStatus.SEARCHING_GUIDE
        assert sky.client.guide_pulse_calls, "no guide pulse was sent"
        assert sky.client.axis_move_calls == []  # no GOTO while tracking
        assert sky.distance_to_target() <= 6.0  # fine tolerance + centroiding
        # #44 / "never start/stop tracking itself": tracking untouched, never stopped.
        assert sky.client.tracking is True
        assert sky.client.tracking_enable_calls == 0
        assert sky.client.emergency_stop_calls == 0
        assert all(ms <= 5000 for _d, ms in sky.client.guide_pulse_calls)
        assert all(ms >= 20 for _d, ms in sky.client.guide_pulse_calls)

    def test_every_measurement_uses_a_frame_exposed_after_the_last_pulse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sky = _Sky(monkeypatch, start=(250.0, 170.0))

        result = sky.reacquire()

        assert result.failure_reason is None, result
        assert sky.pulses
        for _start, end in sky.pulses:
            assert any(s >= end for s in sky.frame_starts), f"nothing measured after {end}"
        for frame_start in sky.frame_starts:  # no measured exposure overlaps a pulse
            assert not any(a < frame_start < b for a, b in sky.pulses), frame_start

    def test_a_slow_guide_rate_is_calibrated_with_longer_probes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0.5x sidereal: 1000 ms moves the star 1.1 px here; the probes escalate from a short
        first one (<= 5 s per direction) until it moved >= 4 px."""
        sky = _Sky(monkeypatch, start=(240.0, 165.0), guide=GuidePulseScenario(rate_x=0.5))

        result = sky.reacquire()

        assert result.failure_reason is None, result
        assert sky.client.guide_pulse_calls[:4] == [
            ("west", 250),
            ("west", 750),
            ("west", 1000),
            ("west", 3000),
        ]
        assert sky.distance_to_target() <= 6.0

    def test_without_a_fresh_frame_source_the_latest_frame_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interim (until S6.7): no settled-frame waiter wired -> the latest frame after the
        policy's settle, as on the S6.0b angular path."""
        sky = _Sky(monkeypatch, start=(250.0, 170.0))

        result = sky.reacquire(fresh_frames=False)

        assert result.failure_reason is None, result
        assert sky.frame_starts == []


def _sweep_cases(seed: int, count: int) -> list[tuple[tuple[float, float], float, float, float]]:
    """Seeded (start, rotation, mirror, RA scale): starts 30..70 px from the predicted
    (210, 150), any camera rotation, either RA image direction, RA scale 0.5..1 (cos dec)."""
    rng = random.Random(seed)
    cases = []
    for _ in range(count):
        radius, angle = rng.uniform(30.0, 70.0), rng.uniform(0.0, 2.0 * math.pi)
        start = (210.0 + radius * math.cos(angle), 150.0 + radius * math.sin(angle))
        cases.append(
            (start, rng.uniform(0.0, 360.0), rng.choice((1.0, -1.0)), rng.uniform(0.5, 1.0))
        )
    return cases


class TestConvergenceWithAnyCameraGeometry:
    """The 2-D solve on per-direction guide-pulse responses: camera rotation, mirroring, unequal
    RA/Dec image scales and an asymmetric east/west guide response all converge."""

    @pytest.mark.parametrize(("start", "rot_deg", "ra_sign", "ra_factor"), _sweep_cases(39, 12))
    def test_the_star_returns_to_mains_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
        start: tuple[float, float],
        rot_deg: float,
        ra_sign: float,
        ra_factor: float,
    ) -> None:
        guide = GuidePulseScenario(rate_x=1.0, direction_rate_x={"east": 0.8})
        sky = _Sky(
            monkeypatch,
            start=start,
            guide=guide,
            rot_deg=rot_deg,
            ra_sign=ra_sign,
            ra_factor=ra_factor,
        )

        result = sky.reacquire()

        assert result.failure_reason is None, (result, sky.star())
        assert sky.distance_to_target() <= 6.0, sky.star()
        assert sky.client.axis_move_calls == []
        assert sky.client.tracking is True


class TestUpFrontDecision:
    def test_astronomical_on_0_4_1_is_refused_before_anything_is_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sky = _Sky(monkeypatch, start=(250.0, 170.0), guide_api=False)

        result = sky.reacquire()

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == ASTRONOMICAL_REACQUISITION_UNAVAILABLE
        assert result.detail == ASTRONOMICAL_NEEDS_GUIDE_PULSES
        assert sky.client.axis_move_calls == []
        assert sky.client.guide_pulse_calls == []
        assert sky.client.tracking is True  # tracking never stopped to make a GOTO possible

    def test_an_unreadable_tracking_state_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sky = _Sky(monkeypatch, start=(250.0, 170.0))

        result = sky.reacquire(tracking_state=lambda: None)

        assert result.failure_reason == ASTRONOMICAL_REACQUISITION_UNAVAILABLE
        assert result.detail == TRACKING_STATE_UNKNOWN
        assert sky.client.guide_pulse_calls == [] and sky.client.axis_move_calls == []

    @pytest.mark.parametrize("mode", [OperatingMode.TERRESTRIAL, OperatingMode.ASTRONOMICAL])
    def test_tracking_off_keeps_the_angular_path_even_with_0_5_0(
        self, monkeypatch: pytest.MonkeyPatch, mode: OperatingMode
    ) -> None:
        """Terrestrial (tracking OFF, #44) -- and Astronomical with tracking OFF -- use the
        S6.0b angular path unchanged; no guide pulse is ever sent."""
        sky = _Sky(monkeypatch, start=(250.0, 160.0), tracking=False)

        result = sky.reacquire(mode)

        assert result.failure_reason is None, result
        assert sky.client.axis_move_calls, "the angular path never moved"
        assert sky.client.guide_pulse_calls == []
        assert sky.client.tracking is False

    def test_terrestrial_without_a_mount_align_matrix_fails_explicitly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sky = _Sky(monkeypatch, start=(250.0, 160.0), tracking=False)
        prior_main, registration = _registration()

        result = FocusedStarAcquisition(roi_size=(16, 12)).attempt_guide_reacquisition(
            sky.render,
            mount=sky.mount,
            guide_calibration=None,
            registration=registration,
            prior_main=prior_main,
            clock=sky.clock,
            operating_mode=OperatingMode.TERRESTRIAL,
        )

        assert result.failure_reason == "no_guide_calibration"


class TestOnStepAdapterOutcomesAreSurfaced:
    def test_a_refusal_reason_reaches_the_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sky = _Sky(
            monkeypatch,
            start=(250.0, 170.0),
            guide=GuidePulseScenario(rate_x=1.0, meridian_phase="hard_stop"),
        )

        result = sky.reacquire()

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == "mount_correction_rejected"
        # The refusal first, then OnStepAdapter's warning (hard_stop is past the flip boundary).
        assert result.detail == (
            "guide pulse refused at meridian phase hard_stop; "
            "OnStepAdapter warnings: meridian_flip_required"
        )
        assert sky.client.guide_chunks_issued == []

    def test_a_flip_warning_is_reported_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sky = _Sky(
            monkeypatch,
            start=(250.0, 170.0),
            guide=GuidePulseScenario(rate_x=1.0, meridian_phase="flip_required"),
        )

        result = sky.reacquire()

        assert result.failure_reason is None, result
        assert result.detail is not None and "meridian_flip_required" in result.detail

    def test_an_emergency_stop_after_a_failed_chunk_is_reported_prominently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A chunk fails after calibration (during the correction): 0.5.0 stops the mount
        (tracking OFF) -- the result says so first, never as a mere refusal."""
        sky = _Sky(monkeypatch, start=(250.0, 170.0))
        recording = sky.client.guide_pulse

        def fail_after_calibration(direction: str, duration_ms: int, **kwargs: float) -> object:
            if len(sky.client.guide_pulse_calls) == 12:  # 4 directions x 3 probes done
                sky.guide.chunk_errors.append(
                    TimeoutError("INDI guide pulse TELESCOPE_TIMED_GUIDE_WE did not complete")
                )
            return recording(direction, duration_ms, **kwargs)

        monkeypatch.setattr(sky.client, "guide_pulse", fail_after_calibration)

        result = sky.reacquire()

        assert len(sky.client.guide_pulse_calls) == 13
        assert sky.client.emergency_stop_calls == 1 and sky.client.tracking is False
        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == MOUNT_TRACKING_STOPPED
        assert result.detail is not None
        assert "did not complete" in result.detail and "NOT tracking" in result.detail

    def test_an_emergency_stop_during_calibration_is_reported_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sky = _Sky(
            monkeypatch,
            start=(250.0, 170.0),
            guide=GuidePulseScenario(
                rate_x=1.0,
                chunk_errors=[
                    None,
                    RuntimeError("INDI rejected guide pulse TELESCOPE_TIMED_GUIDE_WE"),
                ],
            ),
        )

        result = sky.reacquire()

        assert result.failure_reason == MOUNT_TRACKING_STOPPED
        assert result.detail is not None and "INDI rejected guide pulse" in result.detail


class TestStopDuringAGuidePulse:
    def test_stop_ends_the_reacquisition_and_says_tracking_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sky = _Sky(monkeypatch, start=(250.0, 170.0))
        recording = sky.client.guide_pulse
        pressed: list[int] = []

        def press_stop() -> None:
            sky.mount.abort()  # S6.0c: lock-free, returns at once
            # Barrier: the stop worker's emergency stop reached the controller.
            assert _until(lambda: not sky.client.tracking), "the stop never reached the mount"

        def stop_during_the_third_pulse(
            direction: str, duration_ms: int, **kwargs: float
        ) -> object:
            if len(sky.client.guide_pulse_calls) == 2 and not pressed:
                pressed.append(len(sky.client.guide_chunks_issued))
                sky.clock.call_later(0.2, press_stop)
            return recording(direction, duration_ms, **kwargs)

        monkeypatch.setattr(sky.client, "guide_pulse", stop_during_the_third_pulse)
        try:
            result = sky.reacquire()
        finally:
            sky.join_stop_worker()

        assert pressed
        assert len(sky.client.guide_pulse_calls) == 3  # nothing after the Stop
        assert [ms for _d, ms in sky.client.guide_chunks_issued[pressed[0] :]] == [500]
        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == MOUNT_TRACKING_STOPPED
        assert result.detail is not None and result.detail.startswith("stopped by the user")


class TestReviewFixes:
    def test_a_stop_between_pulses_says_the_mount_stopped_tracking(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review fix 1: Stop pressed between two pulses (0.5.0's emergency stop: ABORT +
        TRACK_OFF); the next pulse is refused unsent -- reported as mount_tracking_stopped."""
        sky = _Sky(monkeypatch, start=(250.0, 170.0))
        recording = sky.client.guide_pulse

        def stop_after_the_first_pulse(direction: str, duration_ms: int, **kwargs: float) -> object:
            result = recording(direction, duration_ms, **kwargs)
            if len(sky.client.guide_pulse_calls) == 1:
                sky.mount.abort()
                sky.join_stop_worker()
            return result

        monkeypatch.setattr(sky.client, "guide_pulse", stop_after_the_first_pulse)

        result = sky.reacquire()

        assert len(sky.client.guide_pulse_calls) == 1
        assert result.failure_reason == MOUNT_TRACKING_STOPPED
        assert result.detail is not None and "NOT tracking" in result.detail

    def test_switching_away_from_astronomical_stops_the_guide_pulses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review fix 5 (A2, #44 intent): the mode is re-read between pulses."""
        sky = _Sky(monkeypatch, start=(250.0, 170.0))
        mode = [OperatingMode.ASTRONOMICAL]
        recording = sky.client.guide_pulse

        def switch_after_two_pulses(direction: str, duration_ms: int, **kwargs: float) -> object:
            result = recording(direction, duration_ms, **kwargs)
            if len(sky.client.guide_pulse_calls) == 2:
                mode[0] = OperatingMode.TERRESTRIAL
            return result

        monkeypatch.setattr(sky.client, "guide_pulse", switch_after_two_pulses)

        result = sky.reacquire(current_mode=lambda: mode[0])

        assert len(sky.client.guide_pulse_calls) == 2
        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == OPERATING_MODE_CHANGED
        assert result.detail is not None and "Astronomical" in result.detail

    def test_a_mode_switch_during_the_correction_also_stops(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sky = _Sky(monkeypatch, start=(250.0, 170.0))
        mode = [OperatingMode.ASTRONOMICAL]
        recording = sky.client.guide_pulse

        def switch_after_calibration(direction: str, duration_ms: int, **kwargs: float) -> object:
            result = recording(direction, duration_ms, **kwargs)
            if len(sky.client.guide_pulse_calls) == 13:  # calibration (12) + first correction
                mode[0] = OperatingMode.TERRESTRIAL
            return result

        monkeypatch.setattr(sky.client, "guide_pulse", switch_after_calibration)

        result = sky.reacquire(current_mode=lambda: mode[0])

        assert len(sky.client.guide_pulse_calls) == 13
        assert result.failure_reason == OPERATING_MODE_CHANGED
