"""S6.0b (#39 AC 5, audit P01): guide-assisted reacquisition end to end against the PRODUCTION
OnStep adapter on the #51 simulator.

`FocusedStarAcquisition.attempt_guide_reacquisition` (detect -> resolve identity -> confidence
gate -> `CollimationRecenterPolicy`) -> the real `OnStepMountPulseAdapter` -> the simulated
OnStepAdapter controller's axis motion (0.4.1 semantics, unchanged in the pinned 0.5.0: no timed
pulse for this path; finite axis GOTO of 30"..10 deg). Guide frames
are rendered from where the simulated controller REALLY pointed, so the star only comes back if
the mount received the right moves. The Guide calibration matrix is in S6.0's unit (equivalent
ms at `calibration_center_rate_x` x sidereal), modelled here from the settings owner.

Before the fix every reacquisition on this adapter ended `mount_correction_rejected` without
moving anything, and the adapter's reason was dropped.
"""

from __future__ import annotations

import math
import random

import numpy as np
import pytest
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, AxisResponse, CalibrationMatrix, MountAxis
from astrotool_core.mount.movement_sizing import SIDEREAL_ARCSEC_PER_S
from astrotool_core.onstep import OnStepMountPulseAdapter
from astrotool_core.registration.geometry import polygon_centroid, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)
from astrotool_core.testing import OnStepScenario, make_simulated_onstep_connection
from astrotool_core.testing.frame_factory import single_star_image
from astrotool_core.testing.sim_onstep import AXIS_STATE_REFUSED, SimulatedOnStepIndiClient
from astrotool_core.timing import FakeClock
from collimation_tool.application.star_acquisition import AcquisitionStatus, FocusedStarAcquisition

_MAIN_SHAPE = (80, 100)  # height, width
_GUIDE_SHAPE = (300, 400)
_GUIDE_ARCSEC_PER_PX = 6.6
_UNIT_ARCSEC_PER_MS = (
    MountAlignmentSettings().calibration_center_rate_x * SIDEREAL_ARCSEC_PER_S / 1000.0
)
_SIGN = {AxisDirection.POSITIVE: 1.0, AxisDirection.NEGATIVE: -1.0}


def _registration() -> tuple[OpticalPrior, CrossCameraRegistrationResult]:
    prior_main = OpticalPrior(
        name="main",
        sensor_width_px=_MAIN_SHAPE[1],
        sensor_height_px=_MAIN_SHAPE[0],
        pixel_scale_arcsec=1.0,
    )
    scale = 0.25
    polygon = rect_polygon(
        _MAIN_SHAPE[1] * scale,
        _MAIN_SHAPE[0] * scale,
        center=(200.0, 150.0),
        rotation_deg=0.0,
    )
    registration = CrossCameraRegistrationResult(
        method=RegistrationMethod.TERRESTRIAL,
        status=RegistrationStatus.OK_OVERLAP,
        rotation_deg=0.0,
        scale=scale,
        polygon_a_in_b=polygon,
        confidence=0.9,
    )
    return prior_main, registration


class _GuideSky:
    """The production adapter on the simulator; the guide star follows the controller."""

    def __init__(
        self,
        start: tuple[float, float],
        *,
        tracking: bool = False,
        ra_gain: float = 1.0,
        dec_gain: float = 1.0,
    ) -> None:
        """`ra_gain`/`dec_gain`: the REAL image response relative to the calibration matrix
        (e.g. a calibration made at another declination, or simply inaccurate)."""
        self.clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, tracking=tracking), clock=self.clock
        )
        self.mount = OnStepMountPulseAdapter(connection)
        self.mount.connect()
        self.client: SimulatedOnStepIndiClient = made[0]
        self.ha0, self.dec0 = self.client.ha_deg, self.client.dec_deg
        self.start = start
        self.ra_gain, self.dec_gain = ra_gain, dec_gain

    def star(self) -> tuple[float, float]:
        ra = (self.client.ha_deg - self.ha0) * 3600.0 * self.ra_gain
        dec = (self.client.dec_deg - self.dec0) * 3600.0 * self.dec_gain
        return (
            self.start[0] + ra / _GUIDE_ARCSEC_PER_PX,
            self.start[1] + dec / _GUIDE_ARCSEC_PER_PX,
        )

    def render(self) -> np.ndarray:
        x, y = self.star()
        return single_star_image(_GUIDE_SHAPE, x=x, y=y, peak=3000.0, sigma=2.0, background=100.0)

    @staticmethod
    def calibration() -> CalibrationMatrix:
        probe_ms = 2000
        px = probe_ms * _UNIT_ARCSEC_PER_MS / _GUIDE_ARCSEC_PER_PX
        responses = {}
        for axis in MountAxis:
            for direction in AxisDirection:
                signed = _SIGN[direction] * px
                dx, dy = (signed, 0.0) if axis is MountAxis.AXIS1 else (0.0, signed)
                responses[(axis, direction)] = AxisResponse(
                    axis=axis,
                    direction=direction,
                    duration_ms=probe_ms,
                    dx_px=dx,
                    dy_px=dy,
                    px_per_ms=px / probe_ms,
                )
        return CalibrationMatrix(responses=responses)


def _selected_acquisition(**kwargs: float) -> FocusedStarAcquisition:
    acquisition = FocusedStarAcquisition(roi_size=(16, 12), **kwargs)  # type: ignore[arg-type]
    # Star near Main's right edge (90, 40): predicted in Guide at (210, 150).
    acquisition.select(
        single_star_image(_MAIN_SHAPE, x=90.0, y=40.0, peak=3000.0, sigma=2.0, background=100.0)
    )
    return acquisition


class TestGuideReacquisitionOnTheProductionAdapter:
    def test_the_star_is_brought_back_to_mains_field(self) -> None:
        prior_main, registration = _registration()
        sky = _GuideSky(start=(250.0, 160.0))

        result = _selected_acquisition().attempt_guide_reacquisition(
            sky.render,
            mount=sky.mount,
            guide_calibration=sky.calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=sky.clock,
        )

        assert result.failure_reason is None, result
        assert result.status is AcquisitionStatus.SEARCHING_GUIDE
        assert sky.client.axis_move_calls, "the mount never moved"
        assert registration.polygon_a_in_b is not None
        target_x, target_y = polygon_centroid(registration.polygon_a_in_b)
        x, y = sky.star()
        # Fine tolerance (5 px) on the DETECTED centroid, plus centroiding error.
        assert math.hypot(x - target_x, y - target_y) <= 6.0

    def test_a_refusal_carries_the_adapters_reason(self) -> None:
        prior_main, registration = _registration()
        sky = _GuideSky(start=(250.0, 160.0), tracking=True)

        result = _selected_acquisition().attempt_guide_reacquisition(
            sky.render,
            mount=sky.mount,
            guide_calibration=sky.calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=sky.clock,
        )

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == "mount_correction_rejected"
        assert result.detail is not None and AXIS_STATE_REFUSED in result.detail
        assert sky.client.axis_move_calls == []

    def test_a_residual_below_the_mounts_smallest_move_is_not_sent(self) -> None:
        prior_main, registration = _registration()
        assert registration.polygon_a_in_b is not None
        target_x, target_y = polygon_centroid(registration.polygon_a_in_b)
        # 4.4 px = 29" per axis: below OnStep's 30" floor, beyond the 5 px fine tolerance.
        sky = _GuideSky(start=(target_x + 4.4, target_y + 4.4))

        result = _selected_acquisition().attempt_guide_reacquisition(
            sky.render,
            mount=sky.mount,
            guide_calibration=sky.calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=sky.clock,
        )

        assert sky.client.axis_move_calls == []
        # Within the rough tolerance: Main's field centre is reached as closely as the mount allows.
        assert result.status is AcquisitionStatus.SEARCHING_GUIDE, result
        assert result.failure_reason is None


@pytest.mark.parametrize("center_rate_x", [4.0, 16.0])
def test_the_configured_center_rate_is_passed_through(center_rate_x: float) -> None:
    """The configured `calibration_center_rate_x` (MountAlignmentSettings) reaches the policy:
    with a matrix in that unit the first move is exactly the 28 px step (0.35 x the 80 px tracker
    radius) = 184.8" of sky. Read in any other unit it would be off by the ratio."""
    prior_main, registration = _registration()
    sky = _GuideSky(start=(250.0, 160.0))
    unit = center_rate_x * SIDEREAL_ARCSEC_PER_S / 1000.0
    px = 2000 * unit / _GUIDE_ARCSEC_PER_PX
    responses = {
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

    result = _selected_acquisition().attempt_guide_reacquisition(
        sky.render,
        mount=sky.mount,
        guide_calibration=CalibrationMatrix(responses=responses),
        registration=registration,
        prior_main=prior_main,
        clock=sky.clock,
        center_rate_x=center_rate_x,
    )

    assert result.failure_reason is None, result
    first_axis, first_deg = sky.client.axis_move_calls[0]
    assert first_axis == "ra"
    # 50 px = 330" to go: the first move is the 28 px step, 184.8" -- whatever the unit.
    assert abs(first_deg) * 3600.0 == pytest.approx(28.0 * _GUIDE_ARCSEC_PER_PX)


class TestALatchedStop:
    """Coordinator decision on S6.0b: a reacquisition is a new operator command. A Stop from an
    earlier command (e.g. Mount Align's) latches in the adapter until the next command re-arms
    it -- the reacquisition re-arms it (like Mount Align's accepted submit); a Stop pressed
    DURING the reacquisition still stops it."""

    @staticmethod
    def _join_stop_worker(mount: OnStepMountPulseAdapter) -> None:
        worker = mount._stop_worker
        if worker is not None:
            worker.join(timeout=5.0)
            assert not worker.is_alive(), "the stop worker never finished"

    def test_an_earlier_stop_does_not_block_a_new_reacquisition(self) -> None:
        prior_main, registration = _registration()
        sky = _GuideSky(start=(250.0, 160.0))
        sky.mount.abort()  # e.g. the operator pressed Stop in Mount Align earlier
        self._join_stop_worker(sky.mount)

        result = _selected_acquisition().attempt_guide_reacquisition(
            sky.render,
            mount=sky.mount,
            guide_calibration=sky.calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=sky.clock,
        )

        assert result.failure_reason is None, result
        assert sky.client.axis_move_calls, "the latched Stop silently blocked the reacquisition"

    def test_a_stop_during_the_reacquisition_stops_it(self) -> None:
        prior_main, registration = _registration()
        sky = _GuideSky(start=(250.0, 160.0))
        moved_at_stop: list[int] = []

        def frame_then_stop() -> np.ndarray:
            if sky.client.axis_move_calls and not moved_at_stop:
                # The first correction has moved: the operator presses Stop.
                moved_at_stop.append(len(sky.client.axis_move_calls))
                sky.mount.abort()
            return sky.render()

        try:
            result = _selected_acquisition().attempt_guide_reacquisition(
                frame_then_stop,
                mount=sky.mount,
                guide_calibration=sky.calibration(),
                registration=registration,
                prior_main=prior_main,
                clock=sky.clock,
            )
        finally:
            self._join_stop_worker(sky.mount)

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == "mount_correction_rejected"
        assert result.detail is not None and "stopped by the user" in result.detail
        # Nothing moves after the Stop.
        assert moved_at_stop and len(sky.client.axis_move_calls) == moved_at_stop[0]


class TestAFrameSizedReacquisitionStep:
    """Re-review of S6.0b: a fixed 500 ms cap (~60" at 8x, ~9 px here) made a long
    reacquisition crawl and could exhaust the iteration bound. A move may shift the guide image
    per axis by up to 0.35 x the tracker's search radius (80 px -> 28 px), bounded by a quarter
    of the frame's smaller side (300 px -> 75 px)."""

    def test_a_large_offset_is_corrected_in_frame_sized_steps(self) -> None:
        prior_main, registration = _registration()
        # 178 px (1175") from Main's field centre in Guide at (200, 150).
        sky = _GuideSky(start=(40.0, 70.0))

        acquisition = _selected_acquisition(guide_max_position_error_px=200.0)
        result = acquisition.attempt_guide_reacquisition(
            sky.render,
            mount=sky.mount,
            guide_calibration=sky.calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=sky.clock,
        )

        assert result.failure_reason is None, result
        moves = [abs(deg) * 3600.0 for _axis, deg in sky.client.axis_move_calls]
        step_cap_arcsec = 0.25 * _GUIDE_SHAPE[0] * _GUIDE_ARCSEC_PER_PX  # 75 px of image
        assert max(moves) > 61.0, moves  # larger than the fixed 500 ms cap
        assert max(moves) <= step_cap_arcsec + 1e-6, moves
        assert len(moves) <= 12, moves

    def test_a_step_never_exceeds_the_trackers_share_of_the_search_radius(self) -> None:
        prior_main, registration = _registration()
        # 72 px from the predicted (210, 150) -- inside the default 80 px identity radius --
        # and 81 px from Main's field centre (200, 150).
        sky = _GuideSky(start=(270.0, 190.0))

        result = _selected_acquisition().attempt_guide_reacquisition(
            sky.render,
            mount=sky.mount,
            guide_calibration=sky.calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=sky.clock,
        )

        assert result.failure_reason is None, result
        moves = [abs(deg) * 3600.0 for _axis, deg in sky.client.axis_move_calls]
        assert max(moves) > 61.0, moves  # larger than the fixed 500 ms cap
        assert max(moves) <= 28.0 * _GUIDE_ARCSEC_PER_PX + 1e-6, moves  # 0.35 x 80 px radius


def _tracker_margin_starts(seed: int, count: int) -> list[tuple[float, float]]:
    """Seeded starts within the 120 px identity radius of the predicted (210, 150) and at
    least 70 px from it -- several steps from Main's field centre (200, 150)."""
    rng = random.Random(seed)
    starts: list[tuple[float, float]] = []
    while len(starts) < count:
        radius, angle = rng.uniform(70.0, 110.0), rng.uniform(0.0, 2.0 * math.pi)
        x, y = 210.0 + radius * math.cos(angle), 150.0 + radius * math.sin(angle)
        if 20.0 <= x <= 380.0 and 20.0 <= y <= 280.0:
            starts.append((x, y))
    return starts


class TestTheTrackerKeepsTheStarAcrossACorrection:
    """Coordinator review of S6.0b: both axes move before the next measurement, so one
    correction can shift the image by up to sqrt(2) x the per-axis step. With the real response
    larger than the calibration (1.5x on both axes, or 2x on RA only -- e.g. calibrated at another
    declination) a 0.5 x radius per-axis step overshoots the tracker's search radius and the star
    is lost. The per-axis step is 0.35 x the radius (combined <= ~0.5 x radius when calibrated,
    <= ~0.74 x radius at 1.5x). The real `RoiTracker` (lock/search radius 120 px here) decides."""

    @pytest.mark.parametrize(("ra_gain", "dec_gain"), [(1.5, 1.5), (2.0, 1.0)])
    @pytest.mark.parametrize("start", _tracker_margin_starts(17, 12))
    def test_the_star_is_never_lost_by_a_correction(
        self, ra_gain: float, dec_gain: float, start: tuple[float, float]
    ) -> None:
        prior_main, registration = _registration()
        sky = _GuideSky(start=start, ra_gain=ra_gain, dec_gain=dec_gain)

        acquisition = _selected_acquisition(guide_max_position_error_px=120.0)
        result = acquisition.attempt_guide_reacquisition(
            sky.render,
            mount=sky.mount,
            guide_calibration=sky.calibration(),
            registration=registration,
            prior_main=prior_main,
            clock=sky.clock,
        )

        assert sky.client.axis_move_calls, result
        assert result.failure_reason != "target_not_found_guide", (result, sky.star())
