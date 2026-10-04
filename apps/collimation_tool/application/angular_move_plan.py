"""Planning a solved (possibly two-axis) move for a mount that only moves by angle (S6.0/S6.0b).

`solve_screen_move` (astrotool_core.mount) turns a desired image shift into per-axis
durations from the measured calibration matrix -- the full 2-D solve, so an arbitrarily
rotated camera is handled. On a mount without timed pulses those durations are equivalent ms
(see `equivalent_duration`), and each component must become an angular move of at least the
mount's smallest accepted size. This module is the one rule for that, shared by Mount Align's
screen moves and the guide-assisted recenter policy:

* each component converts to arcsec through the single conversion owner and is capped;
* a component below the mount's smallest move cannot be commanded. It is DROPPED only when the
  image error it leaves (its own share of the solved shift, from the measured matrix) is within
  `drop_tolerance` of the requested shift; otherwise the whole move is REFUSED;
* the plan is decided as a whole BEFORE anything is sent -- never a partially executed sequence.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from astrotool_core.mount.axis_calibration import CalibrationMatrix
from astrotool_core.mount.port import AxisDirection, MountAxis

from collimation_tool.application.equivalent_duration import (
    arcsec_to_equivalent_ms,
    equivalent_ms_to_arcsec,
)

#: S6.0: a screen-move component below the mount's smallest angular move may be skipped only
#: when the image error it leaves is at most this fraction of the requested shift. Screen
#: moves are fire-and-forget positioning clicks whose sizes step 5/15/30% of the frame, so a
#: <=25% error stays well inside one size step and the next click corrects it; anything
#: larger is refused up front instead of landing somewhere the user did not ask for.
SCREEN_MOVE_DROP_TOLERANCE = 0.25


@dataclass(frozen=True)
class AngularStep:
    axis: MountAxis
    direction: AxisDirection
    #: The same move in the matrix's equivalent-duration unit (>= 1).
    duration_ms: int
    arcsec: float


@dataclass(frozen=True)
class AngularMovePlan:
    """`steps` to send (empty when `refused`); `dropped` lists the sub-floor components
    (axis, arcsec) that were skipped (or, when `refused`, that made the move impossible)."""

    steps: tuple[AngularStep, ...]
    clamped: bool
    dropped: tuple[tuple[MountAxis, float], ...]
    refused: bool


def plan_angular_move(
    matrix: CalibrationMatrix,
    solved: list[tuple[MountAxis, AxisDirection, int]],
    *,
    target_px: float,
    center_rate_x: float,
    cap_arcsec: float,
    floor_arcsec: float,
    drop_tolerance: float = SCREEN_MOVE_DROP_TOLERANCE,
    axis_cap_arcsec: Mapping[MountAxis, float] | None = None,
) -> AngularMovePlan:
    """Plan `solved` (`solve_screen_move`'s steps for a shift of `target_px`) as angular moves.

    Each component is capped on its own (`axis_cap_arcsec[axis]` when given, else
    `cap_arcsec`), so a cap never pushes a component that reaches the floor below it; the drop
    rule is judged against `target_px`, the full requested shift."""
    kept: list[AngularStep] = []
    clamped = False
    dropped: list[tuple[MountAxis, float]] = []
    residual_dx = residual_dy = 0.0
    for axis, direction, duration_ms in solved:
        arcsec = equivalent_ms_to_arcsec(duration_ms, center_rate_x=center_rate_x)
        if arcsec < floor_arcsec:
            response = matrix.response_for(axis, direction)
            residual_dx += response.dx_px / response.duration_ms * duration_ms
            residual_dy += response.dy_px / response.duration_ms * duration_ms
            dropped.append((axis, arcsec))
            continue
        cap = axis_cap_arcsec[axis] if axis_cap_arcsec is not None else cap_arcsec
        if arcsec > cap:
            clamped, arcsec = True, cap
        equivalent_ms = arcsec_to_equivalent_ms(arcsec, center_rate_x=center_rate_x)
        kept.append(AngularStep(axis, direction, max(1, round(equivalent_ms)), arcsec))
    refused = bool(dropped) and (
        not kept or math.hypot(residual_dx, residual_dy) > drop_tolerance * target_px
    )
    return AngularMovePlan(
        steps=() if refused else tuple(kept),
        clamped=clamped,
        dropped=tuple(dropped),
        refused=refused,
    )
