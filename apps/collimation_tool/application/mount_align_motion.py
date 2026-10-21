"""The one place Mount Align hands a move to its background mount sequence (S6.6a).

Every Mount Align motion -- a calibration step, the unmeasured return after an aborted run, a
RA/Dec nudge and a screen move -- is submitted through these two functions, so the choice of
motion primitive per move is visible in one module: an axis move is a sequence of
`(axis, direction, ms, arcsec)` steps, each an ANGULAR move of `arcsec` through OnStepAdapter
(`ms` is its equivalent duration); S6.8b: `primitive` GUIDE_PULSE makes every step's
`duration_ms` a guide pulse sent while tracking (Astronomical Mount Align). S6.8d (R9): the
timed single-axis pulse (`runner.submit`, `rate_preset`, `park_after`) is gone -- the production
mount never had a timed primitive. No Qt.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from astrotool_core.mount import AxisDirection, MountAxis, MountParkPort, MountPort

from collimation_tool.application.mount_motion import MountAlignPrimitive

MotionStep = tuple[MountAxis, AxisDirection, int] | tuple[MountAxis, AxisDirection, int, float]


class MotionRunner(Protocol):
    """The background mount sequence (`MountTestMoveRunner`)."""

    def submit_sequence(
        self,
        mount_park: MountParkPort,
        mount: MountPort,
        steps: Sequence[MotionStep],
        *,
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
    settle_ms: int,
    primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
) -> bool:
    """One single-axis move (a calibration step or a nudge): an angular move of `arcsec` -- or
    (S6.8b, `primitive` GUIDE_PULSE) `duration_ms` of guide pulses while tracking. An axis move
    without an angular size (`arcsec` None) is submitted as-is and refused by the runner before
    anything is sent. False = not started (a move is already running)."""
    step: MotionStep = (
        (axis, direction, duration_ms)
        if primitive is MountAlignPrimitive.GUIDE_PULSE or arcsec is None
        else (axis, direction, duration_ms, arcsec)
    )
    return submit_motion_sequence(
        runner, mount_park, mount, [step], settle_ms=settle_ms, primitive=primitive
    )


def submit_motion_sequence(
    runner: MotionRunner,
    mount_park: MountParkPort,
    mount: MountPort,
    steps: Sequence[MotionStep],
    *,
    settle_ms: int,
    primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
) -> bool:
    """Several moves back to back (a screen move, the return after an aborted calibration)."""
    return runner.submit_sequence(
        mount_park, mount, steps, settle_ms=settle_ms, **_primitive_kwargs(primitive)
    )
