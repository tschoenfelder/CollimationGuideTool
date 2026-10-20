"""The one place Mount Align hands a move to its background mount sequence (S6.6a).

Every Mount Align motion -- a calibration step, the unmeasured return after an aborted run, a
RA/Dec nudge and a screen move -- is submitted through these two functions, so the choice of
motion primitive per move is visible in one module: a single-axis move with no angular size is
a TIMED pulse (`runner.submit`), everything else is a sequence of `(axis, direction, ms)` or
`(axis, direction, ms, arcsec)` steps (`runner.submit_sequence`, an angular move where an arcsec
size is given). Moved out of the panel unchanged; no Qt. S6.8b: `primitive` GUIDE_PULSE makes
every step's `duration_ms` a guide pulse sent while tracking (Astronomical Mount Align).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from astrotool_core.mount import AxisDirection, MountAxis, MountParkPort, MountPort

from collimation_tool.application.mount_motion import MountAlignPrimitive

MotionStep = tuple[MountAxis, AxisDirection, int] | tuple[MountAxis, AxisDirection, int, float]


class MotionRunner(Protocol):
    """The background mount sequence (`MountTestMoveRunner`)."""

    def submit(
        self,
        mount_park: MountParkPort,
        mount: MountPort,
        axis: MountAxis,
        direction: AxisDirection,
        pulse_ms: int,
        *,
        rate_preset: str | None = None,
        park_after: bool = True,
        settle_ms: int = 0,
    ) -> bool: ...

    def submit_sequence(
        self,
        mount_park: MountParkPort,
        mount: MountPort,
        steps: Sequence[MotionStep],
        *,
        rate_preset: str | None = None,
        park_after: bool = True,
        settle_ms: int = 0,
        primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
    ) -> bool: ...


def _primitive_kwargs(primitive: MountAlignPrimitive) -> dict[str, MountAlignPrimitive]:
    """S6.8b: an axis move is submitted exactly as before (no `primitive` argument); only a
    guide-pulse move names its primitive."""
    return {} if primitive is MountAlignPrimitive.AXIS else {"primitive": primitive}


def submit_axis_move(
    runner: MotionRunner,
    mount_park: MountParkPort,
    mount: MountPort,
    axis: MountAxis,
    direction: AxisDirection,
    duration_ms: int,
    arcsec: float | None,
    *,
    rate_preset: str | None,
    settle_ms: int,
    primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
) -> bool:
    """One single-axis move (a calibration step or a nudge): timed when `arcsec` is None, else
    angular -- or (S6.8b, `primitive` GUIDE_PULSE) `duration_ms` of guide pulses while
    tracking. False = not started (a move is already running)."""
    if primitive is MountAlignPrimitive.GUIDE_PULSE:
        return submit_motion_sequence(
            runner,
            mount_park,
            mount,
            [(axis, direction, duration_ms)],
            rate_preset=rate_preset,
            settle_ms=settle_ms,
            primitive=primitive,
        )
    if arcsec is None:
        return runner.submit(
            mount_park,
            mount,
            axis,
            direction,
            duration_ms,
            rate_preset=rate_preset,
            park_after=False,
            settle_ms=settle_ms,
        )
    return runner.submit_sequence(
        mount_park,
        mount,
        [(axis, direction, duration_ms, arcsec)],
        rate_preset=rate_preset,
        park_after=False,
        settle_ms=settle_ms,
    )


def submit_motion_sequence(
    runner: MotionRunner,
    mount_park: MountParkPort,
    mount: MountPort,
    steps: Sequence[MotionStep],
    *,
    rate_preset: str | None,
    settle_ms: int,
    primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
) -> bool:
    """Several moves back to back (a screen move, the return after an aborted calibration)."""
    return runner.submit_sequence(
        mount_park,
        mount,
        steps,
        rate_preset=rate_preset,
        park_after=False,
        settle_ms=settle_ms,
        **_primitive_kwargs(primitive),
    )
