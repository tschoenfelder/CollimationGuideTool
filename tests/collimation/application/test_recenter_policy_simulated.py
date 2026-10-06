"""S6.0b (#39, audit P01): guide-assisted recentering against the PRODUCTION OnStep adapter.

`CollimationRecenterPolicy` -> the real `OnStepMountPulseAdapter` -> `OnStepConnection` -> the
#51 OnStep simulator (the installed OnStepAdapter's axis motion over INDI -- 0.4.1 semantics,
unchanged in the pinned 0.5.0: no timed pulse for Mount Align, only a finite
degree-target axis GOTO of 30"..10 deg, refused while tracking/parked), on a `FakeClock`. The
star's guide-camera position is computed from what the simulated controller REALLY moved, so the
assertions can only pass if the mount received the right moves.

The Guide calibration matrix is built the way Mount Align stores it on such a mount since S6.0
(787ceed): every `duration_ms`/`px_per_ms` is in "equivalent ms" at the configured
`calibration_center_rate_x` x sidereal. The test models that unit from the settings owner and
the sidereal constant itself -- not from the production conversion it checks.

The defect these pin: the policy pulsed with `pulse_axis`, which this adapter always refuses, so
every reacquisition ended "pulse_rejected" (and the adapter's reason was dropped).
"""

from __future__ import annotations

import math
import random

import pytest
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import (
    AxisDirection,
    AxisResponse,
    CalibrationMatrix,
    CommandResult,
    MountAxis,
)
from astrotool_core.mount.movement_sizing import SIDEREAL_ARCSEC_PER_S
from astrotool_core.onstep import MIN_AXIS_ARCSEC, OnStepMountPulseAdapter
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState
from astrotool_core.testing import OnStepScenario, make_simulated_onstep_connection
from astrotool_core.testing.sim_onstep import AXIS_STATE_REFUSED, SimulatedOnStepIndiClient
from astrotool_core.timing import FakeClock
from collimation_tool.application.recenter_policy import CollimationRecenterPolicy, RecenterConfig

#: The configured default the S6.0 matrix unit is based on (test-side model of that unit).
_DEFAULT_CENTER_RATE_X = MountAlignmentSettings().calibration_center_rate_x
_GUIDE_ARCSEC_PER_PX = 6.6
_SIGN = {AxisDirection.POSITIVE: 1.0, AxisDirection.NEGATIVE: -1.0}


class _CountingAdapter(OnStepMountPulseAdapter):
    """The production adapter, unchanged, with `pulse_axis` calls recorded."""

    pulse_calls: list[tuple[MountAxis, AxisDirection, int]]

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        self.pulse_calls.append((axis, direction, duration_ms))
        return super().pulse_axis(axis, direction, duration_ms, rate_preset=rate_preset)


class _GuideRig:
    """Production adapter on the simulator + a guide camera whose star follows the controller.

    `ra_px_factor` scales RA-axis image motion (e.g. cos(dec) away from the equator);
    `ra_sign`/`dec_sign` flip an axis' image direction (arbitrarily mounted camera); `rot_deg`
    rotates the camera against the mount axes. One sky model (`image_shift_px`) serves both the
    star position and the calibration Mount Align would have measured."""

    def __init__(
        self,
        *,
        start_px: tuple[float, float],
        tracking: bool = False,
        ra_px_factor: float = 1.0,
        ra_sign: float = 1.0,
        dec_sign: float = 1.0,
        center_rate_x: float = _DEFAULT_CENTER_RATE_X,
        rot_deg: float = 0.0,
        arcsec_per_px: float = _GUIDE_ARCSEC_PER_PX,
    ) -> None:
        self.clock = FakeClock()
        self.arcsec_per_px = arcsec_per_px
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, tracking=tracking), clock=self.clock
        )
        self.mount = _CountingAdapter(connection)
        self.mount.pulse_calls = []
        self.mount.connect()
        self.client: SimulatedOnStepIndiClient = made[0]
        self.ha0, self.dec0 = self.client.ha_deg, self.client.dec_deg
        self.start_x, self.start_y = start_px
        self.ra_px_factor = ra_px_factor
        self.ra_sign, self.dec_sign = ra_sign, dec_sign
        self.center_rate_x = center_rate_x
        self.rot = math.radians(rot_deg)
        self.measure_calls = 0

    def image_shift_px(self, ra_arcsec: float, dec_arcsec: float) -> tuple[float, float]:
        """Where a mount motion moves the star in the guide image (x right, y down)."""
        ra_px = self.ra_sign * self.ra_px_factor * ra_arcsec / self.arcsec_per_px
        dec_px = self.dec_sign * dec_arcsec / self.arcsec_per_px
        cos, sin = math.cos(self.rot), math.sin(self.rot)
        return (cos * ra_px - sin * dec_px, sin * ra_px + cos * dec_px)

    def position(self) -> tuple[float, float]:
        dx, dy = self.image_shift_px(
            (self.client.ha_deg - self.ha0) * 3600.0, (self.client.dec_deg - self.dec0) * 3600.0
        )
        return (self.start_x + dx, self.start_y + dy)

    def measure(self) -> TrackingResult:
        self.measure_calls += 1
        x, y = self.position()
        return TrackingResult(state=TrackingState.LOCKED, x=x, y=y, matched_source=None)

    def calibration(self) -> CalibrationMatrix:
        """What Mount Align would have measured on this rig: per equivalent ms (S6.0 unit)."""
        unit_arcsec_per_ms = self.center_rate_x * SIDEREAL_ARCSEC_PER_S / 1000.0
        probe_ms = 2000
        responses = {}
        for axis in MountAxis:
            for direction in AxisDirection:
                arcsec = _SIGN[direction] * probe_ms * unit_arcsec_per_ms
                if axis is MountAxis.AXIS1:
                    dx, dy = self.image_shift_px(arcsec, 0.0)
                else:
                    dx, dy = self.image_shift_px(0.0, arcsec)
                responses[(axis, direction)] = AxisResponse(
                    axis=axis,
                    direction=direction,
                    duration_ms=probe_ms,
                    dx_px=dx,
                    dy_px=dy,
                    px_per_ms=math.hypot(dx, dy) / probe_ms,
                )
        return CalibrationMatrix(responses=responses)

    def moves_arcsec(self) -> list[float]:
        return [abs(offset_deg) * 3600.0 for _axis, offset_deg in self.client.axis_move_calls]


class TestRecenteringMovesTheProductionAdapter:
    def test_a_correction_moves_the_mount_and_converges(self) -> None:
        rig = _GuideRig(start_px=(40.0, -25.0))
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert rig.client.axis_move_calls, f"the mount never moved: {result}"
        assert result.reason == "within_tolerance", result
        assert result.success is True
        x, y = rig.position()
        assert math.hypot(x, y) <= RecenterConfig().fine_tolerance_px

    def test_every_move_respects_the_adapters_floor(self) -> None:
        rig = _GuideRig(start_px=(40.0, -25.0))
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        policy.center(rig.measure, reference=(0.0, 0.0))

        assert rig.moves_arcsec(), "the mount never moved"
        assert min(rig.moves_arcsec()) >= MIN_AXIS_ARCSEC

    def test_pulse_axis_is_never_called_on_a_mount_without_timed_pulses(self) -> None:
        rig = _GuideRig(start_px=(40.0, -25.0))
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        policy.center(rig.measure, reference=(0.0, 0.0))

        assert rig.mount.pulse_calls == []

    @pytest.mark.parametrize(("ra_sign", "dec_sign"), [(1.0, 1.0), (-1.0, 1.0), (1.0, -1.0)])
    def test_the_measured_signs_pick_the_direction_not_an_assumed_convention(
        self, ra_sign: float, dec_sign: float
    ) -> None:
        rig = _GuideRig(start_px=(-30.0, 20.0), ra_sign=ra_sign, dec_sign=dec_sign)
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.reason == "within_tolerance", result
        assert result.pulses_issued == len(rig.client.axis_move_calls)

    def test_the_settle_after_each_move_waits_on_the_injected_clock(self) -> None:
        rig = _GuideRig(start_px=(40.0, -25.0))
        config = RecenterConfig(settle_ms=750)
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), config, clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.pulses_issued > 0, result
        assert rig.clock.sleeps.count(0.75) == result.pulses_issued


class TestRefusalsAndTheFloor:
    def test_a_refused_move_reports_the_adapters_reason(self) -> None:
        # OnStepAdapter (0.4.1 and 0.5.0) refuses a local axis move while tracking (sim_onstep.py).
        rig = _GuideRig(start_px=(40.0, -25.0), tracking=True)
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.success is False
        assert result.reason == "pulse_rejected"
        assert AXIS_STATE_REFUSED in result.message
        assert rig.client.axis_move_calls == []

    def test_a_correction_below_the_floor_is_skipped_and_never_sent(self) -> None:
        # 4.4 px = 29" per axis (< the 30" floor), but 6.2 px in total (> fine tolerance 5 px).
        rig = _GuideRig(start_px=(4.4, 4.4))
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.reason == "below_mount_minimum_move", result
        assert result.pulses_issued == 0
        assert rig.client.axis_move_calls == []
        assert rig.mount.pulse_calls == []
        assert "30" in result.message
        # Residual within the rough tolerance: reported as a (rough) success.
        assert result.success is True

    def test_a_sub_floor_residual_beyond_the_rough_tolerance_is_within_the_smallest_move(
        self,
    ) -> None:
        # Coordinator decision: beyond a (tight) rough tolerance, but within one smallest move
        # per axis (sqrt(2) x 4.5 px) -- as close as this mount can get, a success that says so.
        rig = _GuideRig(start_px=(4.4, 4.4))
        config = RecenterConfig(fine_tolerance_px=1.0, rough_tolerance_px=2.0)
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), config, clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.reason == "below_mount_minimum_move"
        assert result.success is True
        assert "within the mount's smallest move" in result.message
        assert rig.client.axis_move_calls == []

    def test_a_small_sub_floor_component_is_dropped_and_the_rest_is_sent(self) -> None:
        # RA: 20 px = 132" (sent); Dec: 4.0 px = 26.4" (below the floor) leaves 4.0 of 20.4 px
        # (20%) -- within the closed loop's drop tolerance, so RA alone is sent.
        rig = _GuideRig(start_px=(20.0, 4.0))
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert [axis for axis, _deg in rig.client.axis_move_calls] == ["ra", "ra"]
        assert min(rig.moves_arcsec()) >= MIN_AXIS_ARCSEC
        assert result.success is True, result

    def test_a_large_sub_floor_component_refuses_the_whole_correction(self) -> None:
        # RA moves the image only half as far per arcsec (cos(dec) = 0.5): its 4.0 px need
        # 52.8" (sendable), but Dec's 4.4 px = 29" is below the floor and is 74% of the shift --
        # sending RA alone would leave most of the error, so nothing is sent.
        rig = _GuideRig(start_px=(4.0, 4.4), ra_px_factor=0.5)
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.reason == "below_mount_minimum_move", result
        assert rig.client.axis_move_calls == []
        assert "AXIS2 29.0" in result.message


class TestTheConfiguredCenterRate:
    def test_moves_are_sized_in_the_configured_unit(self) -> None:
        # A non-default center rate: the matrix's equivalent ms are 16x sidereal ms, so the
        # 500 ms step cap is 120.3" and a 40 px (264") offset is first corrected by the cap.
        rig = _GuideRig(start_px=(40.0, 0.0), center_rate_x=16.0)
        policy = CollimationRecenterPolicy(
            rig.mount, rig.calibration(), clock=rig.clock, center_rate_x=16.0
        )

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.reason == "within_tolerance", result
        assert rig.moves_arcsec()[0] == pytest.approx(500 * 16.0 * SIDEREAL_ARCSEC_PER_S / 1000)

    def test_a_cap_below_the_mounts_smallest_move_is_reported_as_such(self) -> None:
        # 2x sidereal: the 500 ms cap is 15" -- below the 30" floor, so no move could ever be
        # sent; that is a configuration problem and is named as one.
        rig = _GuideRig(start_px=(40.0, -25.0), center_rate_x=2.0)
        policy = CollimationRecenterPolicy(
            rig.mount, rig.calibration(), clock=rig.clock, center_rate_x=2.0
        )

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.reason == "move_cap_below_mount_minimum", result
        assert result.success is False
        assert "calibration_center_rate_x" in result.message
        assert rig.client.axis_move_calls == []


#: A correction can stop short only by what the mount cannot command: per axis less than its
#: smallest move, i.e. at most two sub-floor components (2 x 30" = 60") of residual.
_FLOOR_LIMITED_RESIDUAL_PX = 2 * MIN_AXIS_ARCSEC / _GUIDE_ARCSEC_PER_PX


class TestARotatedCameraConverges:
    """Review of S6.0b: the camera is rotated arbitrarily against the mount axes (and may see an
    axis inverted). The correction must come from the measured 2-D response of BOTH axes, not
    from mapping image x to RA and y to Dec."""

    @pytest.mark.parametrize("rot_deg", [0.0, 30.0, 45.0, 60.0, 90.0, 135.0])
    @pytest.mark.parametrize(
        ("ra_sign", "dec_sign"), [(1.0, 1.0), (-1.0, 1.0), (1.0, -1.0), (-1.0, -1.0)]
    )
    def test_the_star_is_brought_to_the_reference(
        self, rot_deg: float, ra_sign: float, dec_sign: float
    ) -> None:
        rig = _GuideRig(start_px=(40.0, -25.0), rot_deg=rot_deg, ra_sign=ra_sign, dec_sign=dec_sign)
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        x, y = rig.position()
        assert result.success is True, result
        assert result.reason in ("within_tolerance", "below_mount_minimum_move"), result
        assert math.hypot(x, y) <= _FLOOR_LIMITED_RESIDUAL_PX, (result, (x, y))
        # 47 px = 311" of sky at <= 60" per move: a handful of moves per axis, never wandering.
        assert len(rig.client.axis_move_calls) <= 12, rig.client.axis_move_calls
        assert min(rig.moves_arcsec()) >= MIN_AXIS_ARCSEC


def _sweep_cases(seed: int, count: int) -> list[tuple[float, float, float, float, float]]:
    """Deterministic (seeded) cases: RA image-scale factor (cos(dec) away from the equator),
    rotation, Dec mirroring, and a starting error several per-move caps wide."""
    rng = random.Random(seed)
    cases = []
    for _ in range(count):
        ra_factor = rng.choice([1.0, 0.7, 0.5])
        rot_deg = rng.uniform(0.0, 360.0)
        dec_sign = rng.choice([1.0, -1.0])
        radius, angle = rng.uniform(10.0, 200.0), rng.uniform(0.0, 2.0 * math.pi)
        cases.append(
            (ra_factor, rot_deg, dec_sign, radius * math.cos(angle), radius * math.sin(angle))
        )
    return cases


class TestUnequalAxisScales:
    """Re-review of S6.0b: away from the equator RA moves the image cos(dec) times as far per
    arcsec as Dec. Scaling the whole 2-D correction to the cap pushed the Dec component below
    the mount's smallest move and its share of the SCALED target refused the whole correction
    -- nothing sent, even for a 182 px error (reviewer's sweep4/sweep5, made deterministic)."""

    def test_the_reviewers_repro_moves_and_converges(self) -> None:
        # sweep4.py: 6.6"/px, RA factor 0.5, rotation 210 deg, start (-75.4, -165.4).
        rig = _GuideRig(start_px=(-75.4, -165.4), ra_px_factor=0.5, rot_deg=210.0)
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert rig.client.axis_move_calls, f"nothing was sent: {result}"
        x, y = rig.position()
        assert math.hypot(x, y) < math.hypot(-75.4, -165.4) / 2, result

    @pytest.mark.parametrize(
        ("ra_factor", "rot_deg", "dec_sign", "start_x", "start_y"), _sweep_cases(3, 60)
    )
    def test_the_error_always_shrinks_and_nothing_is_refused_unsent(
        self, ra_factor: float, rot_deg: float, dec_sign: float, start_x: float, start_y: float
    ) -> None:
        """Default per-move cap (60"): a large error may need more corrections than the
        iteration bound allows, but every run makes progress and none is a zero-move refusal
        while the error is beyond tolerance."""
        rig = _GuideRig(
            start_px=(start_x, start_y), ra_px_factor=ra_factor, rot_deg=rot_deg, dec_sign=dec_sign
        )
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        start = math.hypot(start_x, start_y)
        x, y = rig.position()
        assert not (result.pulses_issued == 0 and result.reason == "below_mount_minimum_move"), (
            result
        )
        assert result.reason in ("within_tolerance", "below_mount_minimum_move", "max_pulses"), (
            result
        )
        assert math.hypot(x, y) < start, result
        if result.reason != "max_pulses":
            assert math.hypot(x, y) <= _FLOOR_LIMITED_RESIDUAL_PX, result

    @pytest.mark.parametrize(
        ("ra_factor", "rot_deg", "dec_sign", "start_x", "start_y"), _sweep_cases(5, 60)
    )
    def test_with_a_frame_sized_step_every_case_converges(
        self, ra_factor: float, rot_deg: float, dec_sign: float, start_x: float, start_y: float
    ) -> None:
        """With the reacquisition's frame-sized step (40 px here), a 200 px error converges
        within the iteration bound."""
        rig = _GuideRig(
            start_px=(start_x, start_y), ra_px_factor=ra_factor, rot_deg=rot_deg, dec_sign=dec_sign
        )
        policy = CollimationRecenterPolicy(
            rig.mount, rig.calibration(), clock=rig.clock, max_step_px=40.0
        )

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        x, y = rig.position()
        assert result.success is True, result
        assert result.reason in ("within_tolerance", "below_mount_minimum_move"), result
        assert math.hypot(x, y) <= _FLOOR_LIMITED_RESIDUAL_PX, result
        assert min(rig.moves_arcsec()) >= MIN_AXIS_ARCSEC


class TestAFinePlateScale:
    """Coordinator decision on S6.0b: at ~1"/px the mount's 30" smallest move is 30 px -- more
    than the 20 px rough tolerance. A star within one smallest move of the target (per axis) is
    as close as the mount can bring it: a success, with a message that says why."""

    def test_a_star_within_the_smallest_move_is_a_success(self) -> None:
        # 25 px = 25" per axis (each below the 30" floor), 35.4 px in total: beyond the 20 px
        # rough tolerance, within sqrt(2) x 30 px = 42.4 px.
        rig = _GuideRig(start_px=(25.0, 25.0), arcsec_per_px=1.0)
        policy = CollimationRecenterPolicy(rig.mount, rig.calibration(), clock=rig.clock)

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        assert result.reason == "below_mount_minimum_move", result
        assert result.success is True, result
        assert "within the mount's smallest move" in result.message
        assert rig.client.axis_move_calls == []

    @pytest.mark.parametrize("rot_deg", [0.0, 45.0, 210.0])
    def test_a_large_error_ends_within_the_smallest_move(self, rot_deg: float) -> None:
        rig = _GuideRig(start_px=(-120.0, 90.0), arcsec_per_px=1.0, rot_deg=rot_deg)
        policy = CollimationRecenterPolicy(
            rig.mount, rig.calibration(), clock=rig.clock, max_step_px=60.0
        )

        result = policy.center(rig.measure, reference=(0.0, 0.0))

        x, y = rig.position()
        assert result.success is True, result
        assert math.hypot(x, y) <= math.sqrt(2.0) * MIN_AXIS_ARCSEC, (result, (x, y))
