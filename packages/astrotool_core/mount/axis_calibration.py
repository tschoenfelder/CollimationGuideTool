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

import math
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

    @property
    def magnitude_px(self) -> float:
        return math.hypot(self.dx_px, self.dy_px)

    @property
    def angle_degrees(self) -> float:
        """Direction of the measured displacement in frame space, as
        degrees counterclockwise from image +x (standard array axes: x
        right, y down) — i.e. atan2(dy, dx), normalized to [0, 360).
        Which way the mount's pulse points *in the picture*, not any
        real-sky bearing. 0.0 for a zero-length response (no motion
        detected) since the direction is undefined."""
        if self.dx_px == 0.0 and self.dy_px == 0.0:
            return 0.0
        return math.degrees(math.atan2(self.dy_px, self.dx_px)) % 360.0


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
    return response_from_positions(axis, direction, pulse_ms, before, after)


def calibrate_axis_multi(
    mount: MountPort,
    axis: MountAxis,
    direction: AxisDirection,
    *,
    measures: dict[str, PositionMeasurer],
    pulse_ms: int,
) -> dict[str, AxisResponse]:
    """Like `calibrate_axis`, but for a *single* pulse observed through
    several measurers at once (e.g. two cameras watching the same mount
    move) — `calibrate_axis` called once per measurer would pulse the
    mount once per measurer too, which is not the same move.

    All `measures["before"]`-equivalent reads happen first, then one
    pulse, then all "after" reads — every measurer sees the same single
    pulse, not a pulse-per-measurer.
    """
    before = {key: measurer() for key, measurer in measures.items()}
    result = mount.pulse_axis(axis, direction, pulse_ms)
    if not result.accepted:
        raise RuntimeError(
            f"axis_calibration: pulse rejected for {axis.name} {direction.name}: {result.message}"
        )
    after = {key: measurer() for key, measurer in measures.items()}
    return {
        key: response_from_positions(axis, direction, pulse_ms, before[key], after[key])
        for key in measures
    }


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


def response_from_positions(
    axis: MountAxis,
    direction: AxisDirection,
    pulse_ms: int,
    before: tuple[float, float],
    after: tuple[float, float],
) -> AxisResponse:
    """Public so a caller that must split "measure before" / pulse /
    "measure after" across its own scheduling (e.g. `MountTestMovePanel`,
    which measures on the Qt main thread but pulses on a background one
    — see that module's docstring for why) can still build the same
    `AxisResponse` this module's own `calibrate_axis`/`calibrate_axis_multi`
    produce, without duplicating the math."""
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
