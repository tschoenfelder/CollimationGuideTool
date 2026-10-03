"""CollimationRecenterPolicy settle timing (issue #53, S3b -- timing only; its
`pulse_axis` move path is S6.0b).

`TestCharacterizationBeforeS3b` was written and run green BEFORE the clock
injection (real clock, settle kept tiny or never reached) and runs unchanged
afterwards. The fake-clock tests pin the exact settle per accepted pulse.
"""

from __future__ import annotations

import time

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
from astrotool_core.timing import SYSTEM_CLOCK, FakeClock
from collimation_tool.application.recenter_policy import CollimationRecenterPolicy, RecenterConfig

_SIGN = {AxisDirection.POSITIVE: 1.0, AxisDirection.NEGATIVE: -1.0}


def make_calibration(px_per_ms: float = 0.1) -> CalibrationMatrix:
    """+axis1 moves the star +x, +axis2 moves it +y, at `px_per_ms`."""
    responses = {}
    for axis in MountAxis:
        for direction in AxisDirection:
            delta = 100 * px_per_ms * _SIGN[direction]
            dx, dy = (delta, 0.0) if axis is MountAxis.AXIS1 else (0.0, delta)
            responses[(axis, direction)] = AxisResponse(
                axis=axis,
                direction=direction,
                duration_ms=100,
                dx_px=dx,
                dy_px=dy,
                px_per_ms=px_per_ms,
            )
    return CalibrationMatrix(responses=responses)


class MovingMount:
    """Applies each accepted pulse to a simulated star position (no waiting)."""

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
        self.x += response.dx_px / response.duration_ms * duration_ms
        self.y += response.dy_px / response.duration_ms * duration_ms
        return CommandResult(accepted=True)


def _locked(mount: MovingMount) -> TrackingResult:
    return TrackingResult(state=TrackingState.LOCKED, x=mount.x, y=mount.y, matched_source=None)


class TestCharacterizationBeforeS3b:
    def test_a_rejected_pulse_returns_at_once_without_settling(self) -> None:
        calibration = make_calibration()
        mount = MovingMount(calibration, start=(50.0, 0.0), accept=False)
        policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=5000))
        started = time.monotonic()
        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))
        assert time.monotonic() - started < 2.0  # the 5 s settle never ran
        assert result.reason == "pulse_rejected"
        assert result.pulses_issued == 1

    def test_cancel_is_checked_after_each_settle_before_the_next_pulse(self) -> None:
        calibration = make_calibration(px_per_ms=0.01)  # 500 ms cap -> 5 px per pulse
        mount = MovingMount(calibration, start=(50.0, 0.0))
        policy = CollimationRecenterPolicy(mount, calibration, RecenterConfig(settle_ms=10))
        result = policy.center(
            lambda: _locked(mount),
            reference=(0.0, 0.0),
            cancel_check=lambda: len(mount.pulse_log) >= 1,
        )
        assert result.reason == "cancelled"
        assert result.pulses_issued == 1
        assert mount.pulse_log == [(MountAxis.AXIS1, AxisDirection.NEGATIVE, 500)]


class TestFakeClock:
    def test_the_policy_defaults_to_the_real_clock(self) -> None:
        calibration = make_calibration()
        policy = CollimationRecenterPolicy(MovingMount(calibration, (0.0, 0.0)), calibration)
        assert policy._clock is SYSTEM_CLOCK

    def test_every_accepted_pulse_is_followed_by_exactly_one_settle(self) -> None:
        calibration = make_calibration(px_per_ms=0.01)  # 5 px per capped pulse
        mount = MovingMount(calibration, start=(22.0, 0.0))
        clock = FakeClock()
        policy = CollimationRecenterPolicy(
            mount, calibration, RecenterConfig(settle_ms=750), clock=clock
        )
        result = policy.center(lambda: _locked(mount), reference=(0.0, 0.0))
        assert result.reason == "within_tolerance"
        assert result.pulses_issued == 4  # 22 -> 17 -> 12 -> 7 -> 2 px (within 5 px)
        assert clock.sleeps == [0.75] * result.pulses_issued
        assert clock.monotonic() == pytest.approx(0.75 * result.pulses_issued)

    def test_a_zero_settle_never_waits(self) -> None:
        calibration = make_calibration()
        mount = MovingMount(calibration, start=(50.0, 0.0))
        clock = FakeClock()
        policy = CollimationRecenterPolicy(
            mount, calibration, RecenterConfig(settle_ms=0), clock=clock
        )
        assert policy.center(lambda: _locked(mount), reference=(0.0, 0.0)).success
        assert clock.sleeps == []

    def test_a_cancel_raised_during_a_settle_takes_effect_after_it(self) -> None:
        calibration = make_calibration(px_per_ms=0.01)
        mount = MovingMount(calibration, start=(50.0, 0.0))
        clock = FakeClock()
        cancelled = False

        def cancel() -> None:
            nonlocal cancelled
            cancelled = True

        clock.call_at(0.5, cancel)  # inside the first 0.75 s settle
        policy = CollimationRecenterPolicy(
            mount, calibration, RecenterConfig(settle_ms=750), clock=clock
        )
        result = policy.center(
            lambda: _locked(mount), reference=(0.0, 0.0), cancel_check=lambda: cancelled
        )
        assert result.reason == "cancelled"
        assert result.pulses_issued == 1
        assert clock.monotonic() == 0.75  # the settle ran to its end first
