import math

import pytest
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState
from collimation_tool.application.recenter_policy import CollimationRecenterPolicy, RecenterConfig

_RESPONSE_VECTORS = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): (10.0, 0.0),
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): (-10.0, 0.0),
    (MountAxis.AXIS2, AxisDirection.POSITIVE): (0.0, 10.0),
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): (0.0, -10.0),
}


def make_calibration(px_per_ms: float = 0.1, duration_ms: int = 100) -> CalibrationMatrix:
    responses = {
        (axis, direction): AxisResponse(
            axis=axis,
            direction=direction,
            duration_ms=duration_ms,
            dx_px=dx,
            dy_px=dy,
            px_per_ms=px_per_ms,
        )
        for (axis, direction), (dx, dy) in _RESPONSE_VECTORS.items()
    }
    return CalibrationMatrix(responses=responses)


class MovingMount:
    """Test double: applies each accepted pulse to a simulated star position."""

    def __init__(
        self, calibration: CalibrationMatrix, start: tuple[float, float], *, accept: bool = True
    ) -> None:
        self._calibration = calibration
        self.x, self.y = start
        self.pulse_log: list[tuple[MountAxis, AxisDirection, int]] = []
        self._accept = accept

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9999)

    def status(self) -> MountStatus:
        return MountStatus(connected=True, tracking=True, slewing=False)

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        self.pulse_log.append((axis, direction, duration_ms))
        if not self._accept:
            return CommandResult(accepted=False, message="rejected for test")
        response = self._calibration.response_for(axis, direction)
        per_ms_x = response.dx_px / response.duration_ms
        per_ms_y = response.dy_px / response.duration_ms
        self.x += per_ms_x * duration_ms
        self.y += per_ms_y * duration_ms
        return CommandResult(accepted=True)


def _locked(mount: MovingMount) -> TrackingResult:
    return TrackingResult(state=TrackingState.LOCKED, x=mount.x, y=mount.y, matched_source=None)


def test_converges_on_the_dominant_axis_in_one_pulse() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is True
    assert result.reason == "within_tolerance"
    assert result.pulses_issued == 1
    assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.NEGATIVE, 500)]


def test_negative_offset_picks_the_opposing_positive_direction() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(-50.0, 0.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is True
    assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)]


def test_dominant_axis_chosen_by_larger_offset_component() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(10.0, 50.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert mount.pulse_log[0][0] is MountAxis.AXIS2


def test_pulse_duration_is_clamped_to_max_pulse_ms() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(10_000.0, 0.0))
    config = RecenterConfig(settle_ms=0, max_pulse_ms=200)
    policy = CollimationRecenterPolicy(mount, calibration, config)

    policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert mount.pulse_log[0][2] == 200


def test_already_within_tolerance_issues_no_pulses() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(1.0, 1.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is True
    assert result.pulses_issued == 0


def test_star_lost_aborts_immediately() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    lost = TrackingResult(state=TrackingState.LOST, x=50.0, y=0.0, matched_source=None)
    result = policy.center(lambda: lost, reference=(0.0, 0.0))

    assert result.success is False
    assert result.reason == "star_lost"
    assert result.pulses_issued == 0


def test_rejected_pulse_aborts_with_pulse_rejected_reason() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0), accept=False)
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is False
    assert result.reason == "pulse_rejected"
    assert result.pulses_issued == 1


def test_cancel_check_aborts_before_the_next_measurement() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0))
    policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0), cancel_check=lambda: True)

    assert result.success is False
    assert result.reason == "cancelled"
    assert result.pulses_issued == 0


def test_diverging_offset_aborts_after_max_diverge_count() -> None:
    calibration = make_calibration()
    # A mount that moves the star further away every pulse (broken calibration sign).
    positions = iter([50.0, 60.0, 75.0, 95.0])

    class DivergingMount(MovingMount):
        def pulse_axis(
            self,
            axis: MountAxis,
            direction: AxisDirection,
            duration_ms: int,
            *,
            rate_preset: str | None = None,
        ) -> CommandResult:
            self.pulse_log.append((axis, direction, duration_ms))
            self.x = next(positions)
            return CommandResult(accepted=True)

    mount = DivergingMount(calibration, start=(50.0, 0.0))
    policy = CollimationRecenterPolicy(
        mount, calibration, RecenterConfig(settle_ms=0, max_diverge_count=2, max_iterations=10)
    )

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.success is False
    assert result.reason == "diverging"


def test_max_iterations_reports_final_offset_and_pass_fail_by_rough_tolerance() -> None:
    calibration = make_calibration()
    mount = MovingMount(calibration, start=(50.0, 0.0))
    # max_pulse_ms=10 clamps each correction to a 1.0px step (0.1 px/ms x 10ms) —
    # far too small to reach fine_tolerance_px within 2 iterations.
    policy = CollimationRecenterPolicy(
        mount,
        calibration,
        RecenterConfig(settle_ms=0, max_pulse_ms=10, max_iterations=2, rough_tolerance_px=50.0),
    )

    result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

    assert result.reason == "max_pulses"
    assert result.pulses_issued == 2
    assert result.final_offset_px == pytest.approx(48.0)
    assert result.success is True  # within the generous rough tolerance


class TestTimedPathCharacterization:
    """S6.0b: written and run green BEFORE the angular path was added -- a mount that reports
    timed pulses keeps pulsing via `pulse_axis`, sized from the matrix in ms, even when it also
    offers angular moves."""

    class _TimedAndAngularMount(MovingMount):
        min_angular_arcsec = 30.0

        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self.angular_calls: list[tuple[MountAxis, AxisDirection, float]] = []

        def move_angular(
            self, axis: MountAxis, direction: AxisDirection, arcsec: float
        ) -> CommandResult:
            self.angular_calls.append((axis, direction, arcsec))
            return CommandResult(accepted=False, message="must not be used on a timed mount")

    def test_a_timed_mount_with_angular_moves_still_pulses(self) -> None:
        calibration = make_calibration()
        mount = self._TimedAndAngularMount(calibration, start=(50.0, -20.0))
        policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

        assert result.success is True
        assert result.reason == "within_tolerance"
        assert mount.angular_calls == []
        assert mount.pulse_log == [
            (MountAxis.AXIS1, AxisDirection.NEGATIVE, 500),
            (MountAxis.AXIS2, AxisDirection.POSITIVE, 200),
        ]

    def test_a_short_timed_correction_is_sent_however_small(self) -> None:
        # No floor on the timed path: a 0.6 px residual beyond a tight tolerance is a 6 ms pulse.
        calibration = make_calibration()
        mount = self._TimedAndAngularMount(calibration, start=(0.6, 0.0))
        policy = CollimationRecenterPolicy(
            mount, calibration, RecenterConfig(settle_ms=0, fine_tolerance_px=0.5)
        )

        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

        assert result.reason == "within_tolerance"
        assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.NEGATIVE, 6)]

    def test_a_rejected_timed_pulse_is_reported_after_one_attempt(self) -> None:
        calibration = make_calibration()
        mount = self._TimedAndAngularMount(calibration, start=(50.0, 0.0), accept=False)
        policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

        assert (result.success, result.reason, result.pulses_issued) == (
            False,
            "pulse_rejected",
            1,
        )
        assert mount.angular_calls == []


def _rotated_calibration(rot_deg: float, px_per_ms: float = 0.1) -> CalibrationMatrix:
    """+AXIS1 moves the star along +x and +AXIS2 along +y of the MOUNT frame; the camera is
    rotated by `rot_deg` against it."""
    rot = math.radians(rot_deg)
    responses = {}
    for axis in MountAxis:
        for direction in AxisDirection:
            sign = 1.0 if direction is AxisDirection.POSITIVE else -1.0
            mx, my = (sign * 10.0, 0.0) if axis is MountAxis.AXIS1 else (0.0, sign * 10.0)
            dx = math.cos(rot) * mx - math.sin(rot) * my
            dy = math.sin(rot) * mx + math.cos(rot) * my
            responses[(axis, direction)] = AxisResponse(
                axis=axis,
                direction=direction,
                duration_ms=100,
                dx_px=dx,
                dy_px=dy,
                px_per_ms=px_per_ms,
            )
    return CalibrationMatrix(responses=responses)


class TestTimedPathRotation:
    """Review of S6.0b: the TIMED path (frozen in S6.0b) maps image x to AXIS1 and y to AXIS2
    and sizes from one component -- a camera rotated 90 degrees against the mount does not
    converge. Pinned here as a known defect of the timed path (no production timed mount is
    wired today: OnStepAdapter >= 0.4 has no timed pulse); the angular path solves in 2-D."""

    @pytest.mark.xfail(
        strict=True,
        raises=AssertionError,
        reason="timed path maps image x->AXIS1, y->AXIS2 (rotation-blind); frozen in S6.0b",
    )
    def test_a_camera_rotated_90_degrees_converges_on_the_timed_path(self) -> None:
        calibration = _rotated_calibration(90.0)
        mount = MovingMount(calibration, start=(50.0, 0.0))
        policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

        assert result.reason == "within_tolerance", result


class _NoTimedPulseMount(MovingMount):
    """A mount that reports no timed pulses (and, unless given, no angular move either)."""

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(supports_pulse_guiding=False, min_pulse_ms=0, max_pulse_ms=0)


class TestAngularPathWithoutAUsableMotion:
    def test_a_mount_with_neither_timed_nor_angular_moves_is_refused_unsent(self) -> None:
        calibration = make_calibration()
        mount = _NoTimedPulseMount(calibration, start=(50.0, 0.0))
        policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=0))

        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

        assert (result.success, result.reason, result.pulses_issued) == (
            False,
            "pulse_rejected",
            0,
        )
        assert "no timed pulse support" in result.message
        assert mount.pulse_log == []

    def test_a_degenerate_calibration_is_reported_and_nothing_sent(self) -> None:
        calibration = _rotated_calibration(0.0)
        parallel = dict(calibration.responses)
        for direction in AxisDirection:  # AXIS2 measured along x too: not invertible
            parallel[(MountAxis.AXIS2, direction)] = calibration.response_for(
                MountAxis.AXIS1, direction
            )
        matrix = CalibrationMatrix(responses=parallel)
        sent: list[float] = []

        class _AngularMount(_NoTimedPulseMount):
            min_angular_arcsec = 30.0

            def move_angular(
                self, axis: MountAxis, direction: AxisDirection, arcsec: float
            ) -> CommandResult:
                sent.append(arcsec)
                return CommandResult(accepted=True)

        mount = _AngularMount(matrix, start=(50.0, 0.0))
        policy = CollimationRecenterPolicy(mount, matrix, RecenterConfig(settle_ms=0))

        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

        assert result.reason == "calibration_degenerate"
        assert result.success is False
        assert "recalibrate" in result.message
        assert sent == [] and mount.pulse_log == []


class TestBelowTheFloorOnSkewedAxes:
    def test_a_residual_beyond_one_smallest_move_per_axis_is_a_failure(self) -> None:
        """With axes only 60 degrees apart in the image, two sub-floor components (27.7" each)
        add up to 39.8 px -- beyond the rough tolerance AND beyond sqrt(2) x a floor-sized move
        (35.2 px): nothing can be sent, and it is NOT reported as a success."""
        responses = {}
        for axis in MountAxis:
            for direction in AxisDirection:
                sign = 1.0 if direction is AxisDirection.POSITIVE else -1.0
                vx, vy = (10.0, 0.0) if axis is MountAxis.AXIS1 else (5.0, 8.660254)
                responses[(axis, direction)] = AxisResponse(
                    axis=axis,
                    direction=direction,
                    duration_ms=100,
                    dx_px=sign * vx,
                    dy_px=sign * vy,
                    px_per_ms=0.1,
                )
        matrix = CalibrationMatrix(responses=responses)
        sent: list[float] = []

        class _AngularMount(_NoTimedPulseMount):
            min_angular_arcsec = 30.0

            def move_angular(
                self, axis: MountAxis, direction: AxisDirection, arcsec: float
            ) -> CommandResult:
                sent.append(arcsec)
                return CommandResult(accepted=True)

        mount = _AngularMount(matrix, start=(34.5, 19.92))
        policy = CollimationRecenterPolicy(mount, matrix, RecenterConfig(settle_ms=0))

        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))

        assert result.reason == "below_mount_minimum_move", result
        assert result.success is False
        assert "within the mount's smallest move" not in result.message
        assert sent == []
