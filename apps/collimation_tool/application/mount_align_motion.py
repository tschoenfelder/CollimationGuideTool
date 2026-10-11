"""The one place Mount Align hands a move to its background mount sequence (S6.6a).

Every Mount Align motion -- a calibration step, the unmeasured return after an aborted run, a
RA/Dec nudge and a screen move -- is submitted through these two functions, so the choice of
motion primitive per move is visible in one module: a single-axis move with no angular size is
a TIMED pulse (`runner.submit`), everything else is a sequence of `(axis, direction, ms)` or
`(axis, direction, ms, arcsec)` steps (`runner.submit_sequence`, an angular move where an arcsec
size is given). Moved out of the panel unchanged; no Qt.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from astrotool_core.mount import AxisDirection, MountAxis, MountParkPort, MountPort

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
    ) -> bool: ...


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
) -> bool:
    """One single-axis move (a calibration step or a nudge): timed when `arcsec` is None, else
    angular. False = not started (a move is already running)."""
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
) -> bool:
    """Several moves back to back (a screen move, the return after an aborted calibration)."""
    return runner.submit_sequence(
        mount_park, mount, steps, rate_preset=rate_preset, park_after=False, settle_ms=settle_ms
    )
