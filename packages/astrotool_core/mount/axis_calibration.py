"""Axis calibration — pulse-then-measure-response routine.

New: no existing analog in smart_telescope (which never automated this;
guide calibration there is done by eye/external tooling). Sends a bounded
pulse per axis/direction via ``MountPort.pulse_axis`` and turns a
caller-supplied before/after position measurement into a px/ms response
vector.

Deliberately has no dependency on ``astrotool_core.camera`` or
``astrotool_core.target``: how "the current position" is measured (a
camera capture + detector + RoiTracker, in practice) is entirely the
caller's concern, injected as the ``measure`` callback. This keeps the
module small and testable with nothing more than a MountPort and a plain
function.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from astrotool_core.mount.port import AxisDirection, MountAxis, MountPort

PositionMeasurer = Callable[[], tuple[float, float]]


@dataclass(frozen=True)
class AxisResponse:
    """Measured mount response to one pulse on one axis/direction."""

    axis: MountAxis
    direction: AxisDirection
    duration_ms: int
    dx_px: float
    dy_px: float
    px_per_ms: float


@dataclass(frozen=True)
class CalibrationMatrix:
    """Full pulse-response calibration: one AxisResponse per (axis, direction)."""

    responses: dict[tuple[MountAxis, AxisDirection], AxisResponse]

    def response_for(self, axis: MountAxis, direction: AxisDirection) -> AxisResponse:
        return self.responses[(axis, direction)]


def calibrate_axis(
    mount: MountPort,
    axis: MountAxis,
    direction: AxisDirection,
    *,
    measure: PositionMeasurer,
    pulse_ms: int,
) -> AxisResponse:
    """Pulse one axis/direction once and measure the resulting displacement.

    Calls ``measure()`` before and after sending the pulse; the caller is
    responsible for whatever capture/detect/track pipeline that involves.
    """
    before = measure()
    result = mount.pulse_axis(axis, direction, pulse_ms)
    if not result.accepted:
        raise RuntimeError(
            f"axis_calibration: pulse rejected for {axis.name} {direction.name}: {result.message}"
        )
    after = measure()
    return _response_from_positions(axis, direction, pulse_ms, before, after)


def calibrate_axes(
    mount: MountPort,
    *,
    measure: PositionMeasurer,
    pulse_ms: int = 500,
    axes: tuple[MountAxis, ...] = (MountAxis.AXIS1, MountAxis.AXIS2),
    directions: tuple[AxisDirection, ...] = (AxisDirection.POSITIVE, AxisDirection.NEGATIVE),
) -> CalibrationMatrix:
    """Calibrate every (axis, direction) combination in ``axes`` x ``directions``."""
    responses: dict[tuple[MountAxis, AxisDirection], AxisResponse] = {}
    for axis in axes:
        for direction in directions:
            responses[(axis, direction)] = calibrate_axis(
                mount, axis, direction, measure=measure, pulse_ms=pulse_ms
            )
    return CalibrationMatrix(responses=responses)


def _response_from_positions(
    axis: MountAxis,
    direction: AxisDirection,
    pulse_ms: int,
    before: tuple[float, float],
    after: tuple[float, float],
) -> AxisResponse:
    dx = after[0] - before[0]
    dy = after[1] - before[1]
    magnitude = (dx**2 + dy**2) ** 0.5
    px_per_ms = magnitude / pulse_ms if pulse_ms > 0 else 0.0
    return AxisResponse(
        axis=axis,
        direction=direction,
        duration_ms=pulse_ms,
        dx_px=dx,
        dy_px=dy,
        px_per_ms=px_per_ms,
    )
