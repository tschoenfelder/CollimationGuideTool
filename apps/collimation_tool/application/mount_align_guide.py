"""Astronomical Mount Align with guide pulses (S6.8b; user decision 2026-10-11).

In Astronomical mode, with the mount tracking and a mount that reports
`supports_guide_pulses_while_tracking` (OnStepAdapter >= 0.5.0), every Mount Align move --
calibration steps and their returns, RA/Dec nudges, screen moves -- is a guide pulse sent while
tracking (`choose_mount_align_primitive`, `MountMotionExecutor`'s module). Tracking is never
turned off by Mount Align.

Sizes are guide-pulse milliseconds. Their single owner of "how far does one guide-ms move" is
the measured calibration (`CalibrationMatrix.unit` = `GUIDE_PULSE_MS`); a matrix measured with
the other primitive is never used to size these moves, and vice versa (`unit_mismatch`).
Before any measurement exists, sizes come from an explicit ASSUMPTION -- the configured guide
rate seed (`MountAlignmentSettings.guide_rate_seed_x`, OnStepAdapter exposes no guide rate) --
always labelled as such (`seed_note`).

One guide pulse is bounded by the adapter's range (0.5.0: 20..5000 ms, sent in 500 ms chunks); a
longer move is split into even pulses (`split_guide_pulse_ms`) with Stop checked between them.
Mount Align's Stop itself is an immediate EMERGENCY stop (user decision 2026-10-21): the
executor's abort sends ABORT + TRACK_OFF, so the pulse in flight is cut and the mount no longer
tracks -- said so (`STOPPED_NOT_TRACKING`). No Qt; no waits of its own.
"""

from __future__ import annotations

import math
import threading
from typing import cast

from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import (
    AxisDirection,
    CalibrationUnit,
    CommandResult,
    GuidePulsePort,
    MountAxis,
    MountPort,
)
from astrotool_core.mount.movement_sizing import SIDEREAL_ARCSEC_PER_S
from astrotool_core.mount.operating_mode import OperatingMode

from collimation_tool.application.mount_motion import MountAlignPrimitive

#: The motion-path label of a guide-pulse step (`MountPulseOutcome.motion_paths`).
MOTION_GUIDE_PULSE = "guide_pulse"

#: The unit a calibration measured with each primitive is stored in.
UNIT_FOR_PRIMITIVE = {
    MountAlignPrimitive.AXIS: CalibrationUnit.AXIS_MS,
    MountAlignPrimitive.GUIDE_PULSE: CalibrationUnit.GUIDE_PULSE_MS,
}

_UNIT_TEXT = {
    CalibrationUnit.AXIS_MS: "axis moves (tracking off)",
    CalibrationUnit.GUIDE_PULSE_MS: "guide pulses while tracking (astronomical mode)",
}

GUIDE_PULSE_NOT_TRACKING = (
    "astronomical guide-pulse move: the mount is not tracking right now (or is parked) -- "
    "nothing was sent; Mount Align never starts or stops tracking (Mount panel)"
)
GUIDE_PULSE_MODE_CHANGED = (
    "the operating mode is no longer Astronomical -- guide-pulse move not sent"
)
GUIDE_PULSE_UNSUPPORTED = (
    "this mount reports no guide pulses while tracking (needs OnStepAdapter >= 0.5.0) -- "
    "nothing was sent"
)


#: The shared wording when a guide-pulse move ended with the mount no longer tracking.
NOT_TRACKING_ANY_MORE = "the mount is NOT tracking any more, check it"
#: Mount Align's Stop during a guide-pulse move (user decision 2026-10-21: an emergency stop).
STOPPED_NOT_TRACKING = (
    f"Stop is an emergency stop (ABORT + TRACK_OFF) -- {NOT_TRACKING_ANY_MORE}. To restore "
    "tracking, turn it on at the hand controller / OnStep (Mount Align and the Astronomical "
    "mode never turn tracking on themselves); then continue -- guide-pulse moves need it"
)


def unit_mismatch(unit: CalibrationUnit, primitive: MountAlignPrimitive) -> str | None:
    """Why a matrix in `unit` may not size a move made with `primitive` (None = it may)."""
    wanted = UNIT_FOR_PRIMITIVE[primitive]
    if unit is wanted:
        return None
    return (
        f"this calibration was measured with {_UNIT_TEXT[unit]}, but Mount Align now moves "
        f"with {_UNIT_TEXT[wanted]} -- nothing was sent; recalibrate in this mode"
    )


def guide_seed_rate_arcsec_per_s(settings: MountAlignmentSettings) -> float:
    """The ASSUMED guide rate in arcsec per second (see the module docstring)."""
    return settings.guide_rate_seed_x * SIDEREAL_ARCSEC_PER_S


def seed_guide_ms(arcsec: float, settings: MountAlignmentSettings) -> int:
    """The guide-pulse duration that moves `arcsec` at the assumed seed rate."""
    return round(arcsec / guide_seed_rate_arcsec_per_s(settings) * 1000.0)


def seed_note(settings: MountAlignmentSettings) -> str:
    """The label every seed-sized move carries (status line + diagnostics)."""
    return (
        f"sized from the assumed guide rate {settings.guide_rate_seed_x:g}x sidereal "
        "(guide_rate_seed_x; not measured yet)"
    )


def split_guide_pulse_ms(total_ms: int, bounds: tuple[int, int]) -> list[int]:
    """`total_ms` as even guide pulses within `bounds` (min, max): as few pulses as the
    maximum allows, sizes differing by at most 1 ms, so no remainder falls below the minimum.
    [] when `total_ms` is below the shortest pulse (cannot be sent)."""
    shortest, longest = bounds
    if total_ms < max(1, shortest):
        return []
    count = math.ceil(total_ms / longest)
    base, extra = divmod(total_ms, count)
    return [base + (1 if index < extra else 0) for index in range(count)]


def guide_pulse_refusal(
    mode: OperatingMode | None,
    *,
    parked: bool,
    tracking: bool | None,
    mount: MountPort,
) -> str | None:
    """The worker's last check before a guide-pulse move, on a FRESH reading: still
    Astronomical (`mode` None = no mode policy wired, not checked), unparked and tracking, and
    the capability still reported. None = it may be sent."""
    if mode is not None and mode is not OperatingMode.ASTRONOMICAL:
        return GUIDE_PULSE_MODE_CHANGED
    if parked or tracking is not True:
        return GUIDE_PULSE_NOT_TRACKING
    if not mount.capabilities().supports_guide_pulses_while_tracking:
        return GUIDE_PULSE_UNSUPPORTED
    return None


def send_guide_pulse_move(
    mount: MountPort,
    axis: MountAxis,
    direction: AxisDirection,
    total_ms: int,
    cancel: threading.Event,
) -> tuple[CommandResult, int, int]:
    """One Mount Align guide-pulse move of `total_ms`: split into the adapter's pulse range,
    Stop (`cancel`) checked before every pulse. Returns the result, the guide-ms of the pulses
    that COMPLETED, and the guide-ms of a pulse that was sent and then failed (it may have
    moved the mount by any part of it -- unknown, so never counted as done). A failed pulse is
    final; when the mount no longer tracks afterwards (0.5.0 emergency-stops on a failure after
    an issued chunk) the message says so."""
    guiding = cast(GuidePulsePort, mount)
    bounds = guiding.guide_pulse_range_ms
    if bounds is None or not mount.capabilities().supports_guide_pulses_while_tracking:
        return CommandResult(accepted=False, message=GUIDE_PULSE_UNSUPPORTED), 0, 0
    pulses = split_guide_pulse_ms(total_ms, bounds)
    if not pulses:
        return (
            CommandResult(
                accepted=False,
                message=(
                    f"a {total_ms} ms guide pulse is below the mount's shortest guide pulse "
                    f"({bounds[0]} ms) -- nothing was sent"
                ),
            ),
            0,
            0,
        )
    sent_ms = 0
    for sent, pulse_ms in enumerate(pulses):
        if cancel.is_set():
            return (
                CommandResult(
                    accepted=False,
                    message=(
                        f"stopped by the user -- {sent} of {len(pulses)} guide pulses sent"
                    ),
                ),
                sent_ms,
                0,
            )
        result = guiding.guide_pulse(axis, direction, pulse_ms)
        if not result.accepted:
            message = result.message or "guide pulse refused"
            if result.tracking_off:
                message = f"{message} -- {NOT_TRACKING_ANY_MORE}"
            return (
                CommandResult(accepted=False, message=message),
                sent_ms,
                pulse_ms if result.sent else 0,
            )
        sent_ms += pulse_ms
    return CommandResult(accepted=True, message=f"{len(pulses)} guide pulse(s)"), sent_ms, 0
