"""Astronomical-mode guide-assisted reacquisition with tracking-preserving guide pulses
(S6.0d, issue #39; user decision 2026-10-04).

Which motion a reacquisition may use is decided ONCE, up front, from the global operating mode
(its single owner is `TrackingEnforcer.mode`), a FRESH tracking reading and the mount's reported
capability -- never by trying one and falling back:

- Terrestrial (or no mode given): the S6.0b angular path, unchanged (#44 keeps tracking OFF).
- Astronomical, tracking OFF: the same angular path (OnStepAdapter's axis GOTO requires a
  non-tracking mount anyway).
- Astronomical, tracking ON, mount reports `supports_guide_pulses_while_tracking`: guide pulses
  (OnStepAdapter >= 0.5.0 `mount.guide_pulse`), tracking left ON.
- Astronomical, tracking ON, no such capability (the published 0.4.1): refused before anything
  is sent with `ASTRONOMICAL_NEEDS_GUIDE_PULSES`. An axis GOTO would be refused while tracking,
  and stopping tracking to make it possible is not this workflow's decision (#44: tracking is
  only ever changed by the mode policy).
- Astronomical, tracking state not readable just now: refused (fail closed, like #44's gate).

This module never starts or stops tracking.

Guide-pulse calibration. OnStepAdapter 0.5.0 sends durations only (indi_guiding.py:190-194: the
TIMED_GUIDE_NS/WE number is the duration in ms) and exposes no guide rate, so "how far does one
guide-ms move the star in the guide image" can only come from evidence: a small, bounded pulse
per axis direction (west/east/north/south), measured on frames whose exposure STARTED after the
pulse had completed (0.5.0's `guide_pulse` returns only after every chunk's TIMED_GUIDE property
reported completion and a final safety preflight, :196-212). Each direction gets its own response
vector, so camera rotation, mirroring, unequal RA/Dec scales (cos(dec)) and an asymmetric RA
response (with tracking on, east/west guiding slows/speeds the RA drive) all enter the 2-D solve
(`solve_screen_move`) exactly as on the S6.0b angular path. Mount Align's matrix is not reused:
it was measured with axis GOTOs, in equivalent ms at the centering rate, typically in terrestrial
mode at another declination -- a different motion primitive and rate.

Bounds: probes of `GuidePulseCalibrationConfig.probe_ms` (250, 750, 1000, 3000 ms, each within
the adapter-reported 20..5000 ms range; 0.5.0 splits each into <= 500 ms chunks with a safety
check between them) are sent in one direction only until the star has moved
`min_displacement_px`; the response is the cumulative displacement over the cumulative duration.
The first probe is short and every later one is capped from the response measured so far, so the
cumulative excursion stays within `max_displacement_px` (the caller's share of the guide tracker's
search radius) -- a fast guide rate at a fine plate scale cannot jump the star out of the tracker
(review A1). At most 5 s of guiding per direction, 20 s in total, and the four directions are
paired (west then east, north then south), so the star ends near where it started. Too little
motion after the last probe is an explicit failure, not a guess. The calibration is kept for the
rest of the reacquisition session (`FocusedStarAcquisition` instance).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from astrotool_core.mount import (
    MOUNT_BUSY_REASON,
    WORKER_DECISION_FRESH_WAIT_S,
    AxisDirection,
    AxisResponse,
    CalibrationMatrix,
    MountAxis,
    MountParkPort,
    MountPort,
    is_degenerate,
)
from astrotool_core.mount.operating_mode import OperatingMode
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState

#: The exact user-facing refusal when astronomical reacquisition is impossible on this install.
ASTRONOMICAL_NEEDS_GUIDE_PULSES = (
    "astronomical reacquisition needs OnStepAdapter ≥ 0.5.0 (guide pulses while tracking)"
)
TRACKING_STATE_UNKNOWN = (
    "astronomical mode: the mount's tracking state could not be read just now -- "
    f"reacquisition not started ({MOUNT_BUSY_REASON})"
)
MOUNT_NOT_CONNECTED = "the mount is not connected -- connect it (Mount Align) first"

_LOCKED_STATES = (TrackingState.LOCKED, TrackingState.REACQUIRED)

#: Pairs: each direction's probe is undone by the next one's.
_CALIBRATION_ORDER = (
    (MountAxis.AXIS1, AxisDirection.POSITIVE),
    (MountAxis.AXIS1, AxisDirection.NEGATIVE),
    (MountAxis.AXIS2, AxisDirection.POSITIVE),
    (MountAxis.AXIS2, AxisDirection.NEGATIVE),
)


class ReacquisitionMotion(Enum):
    ANGULAR = "angular"
    GUIDE_PULSE = "guide_pulse"


@dataclass(frozen=True)
class MotionDecision:
    """`motion` is None exactly when `refusal` says why nothing may be sent."""

    motion: ReacquisitionMotion | None
    refusal: str | None = None


def read_tracking_for_decision(mount_park: MountParkPort) -> bool | None:
    """A FRESH tracking reading for a decision off the GUI thread (S6.0c: waits up to
    `WORKER_DECISION_FRESH_WAIT_S`), or None when none is available (busy / no mount)."""
    status = mount_park.decision_status(wait_fresh_s=WORKER_DECISION_FRESH_WAIT_S)
    if not status.fresh or not status.available:
        return None
    return status.tracking


def choose_reacquisition_motion(
    mode: OperatingMode | None,
    tracking: Callable[[], bool | None] | None,
    mount: MountPort,
) -> MotionDecision:
    """See the module docstring. `tracking` is only read in Astronomical mode."""
    if mode is not OperatingMode.ASTRONOMICAL:
        return MotionDecision(ReacquisitionMotion.ANGULAR)
    tracking_on = tracking() if tracking is not None else None
    if tracking_on is None:
        return MotionDecision(None, TRACKING_STATE_UNKNOWN)
    if not tracking_on:
        return MotionDecision(ReacquisitionMotion.ANGULAR)
    if mount.capabilities().supports_guide_pulses_while_tracking:
        return MotionDecision(ReacquisitionMotion.GUIDE_PULSE)
    if not mount.status().connected:
        return MotionDecision(None, MOUNT_NOT_CONNECTED)
    return MotionDecision(None, ASTRONOMICAL_NEEDS_GUIDE_PULSES)


@dataclass(frozen=True)
class GuidePulseCalibrationConfig:
    #: Successive probes in one direction (cumulative 0.25, 1, 2, 5 s), stopping once the star
    #: moved `min_displacement_px`. Each must lie within the adapter-reported pulse range.
    probe_ms: tuple[int, ...] = (250, 750, 1000, 3000)
    #: Smallest displacement trusted as a response: with sub-pixel centroiding (~0.1-0.3 px on a
    #: guide star) the rate error stays below ~10%, and the closed loop re-measures anyway.
    min_displacement_px: float = 4.0


@dataclass(frozen=True)
class GuidePulseCalibrationOutcome:
    """`matrix` is None exactly when `failure_reason` is set. `failure_reason` uses
    `FocusedStarAcquisition`'s vocabulary (`mount_correction_rejected`, `mount_tracking_stopped`,
    `target_not_found_guide`, `cancelled`, `guide_pulse_calibration_failed`)."""

    matrix: CalibrationMatrix | None
    failure_reason: str | None = None
    message: str = ""
    warnings: tuple[str, ...] = ()
    pulses_issued: int = 0


def _capped_probe(
    planned_ms: int,
    total_ms: int,
    moved: tuple[float, float],
    max_displacement_px: float | None,
) -> int:
    """The next probe, shortened so the response measured so far predicts a cumulative
    displacement within `max_displacement_px`."""
    magnitude = math.hypot(*moved)
    if max_displacement_px is None or total_ms <= 0 or magnitude <= 0.0:
        return planned_ms
    allowed_total_ms = int(max_displacement_px * total_ms / magnitude)
    return min(planned_ms, allowed_total_ms - total_ms)


def calibrate_guide_pulses(
    mount: MountPort,
    measure: Callable[[], TrackingResult],
    *,
    config: GuidePulseCalibrationConfig | None = None,
    cancel_check: Callable[[], bool] | None = None,
    max_displacement_px: float | None = None,
) -> GuidePulseCalibrationOutcome:
    """Measure the guide-image response to guide pulses in all four directions (see the module
    docstring). `measure` must return a position from a frame captured after the last pulse;
    `max_displacement_px` bounds each direction's cumulative excursion (None = unbounded)."""
    cfg = config or GuidePulseCalibrationConfig()
    guide_pulse = getattr(mount, "guide_pulse", None)
    bounds = getattr(mount, "guide_pulse_range_ms", None)
    if guide_pulse is None or bounds is None:
        return GuidePulseCalibrationOutcome(
            None, "mount_correction_rejected", ASTRONOMICAL_NEEDS_GUIDE_PULSES
        )
    shortest, longest = bounds
    if not cfg.probe_ms or any(not shortest <= ms <= longest for ms in cfg.probe_ms):
        return GuidePulseCalibrationOutcome(
            None,
            "guide_pulse_calibration_failed",
            f"calibration probes {cfg.probe_ms} ms are outside the mount's guide-pulse range "
            f"({shortest}-{longest} ms)",
        )
    warnings: set[str] = set()
    pulses = 0
    responses: dict[tuple[MountAxis, AxisDirection], AxisResponse] = {}

    def failed(reason: str, message: str = "") -> GuidePulseCalibrationOutcome:
        return GuidePulseCalibrationOutcome(None, reason, message, tuple(sorted(warnings)), pulses)

    for axis, direction in _CALIBRATION_ORDER:
        before = measure()
        if before.state not in _LOCKED_STATES or before.x is None or before.y is None:
            return failed("target_not_found_guide")
        total_ms = 0
        moved = (0.0, 0.0)
        for planned in cfg.probe_ms:
            if cancel_check is not None and cancel_check():
                return failed("cancelled")
            probe = _capped_probe(planned, total_ms, moved, max_displacement_px)
            if probe < shortest:
                break  # one more probe would carry the star beyond the allowed excursion
            result = guide_pulse(axis, direction, probe)
            pulses += 1
            warnings.update(result.warnings)
            if not result.accepted:
                reason = (
                    "mount_tracking_stopped"
                    if result.tracking_off
                    else ("mount_correction_rejected")
                )
                return failed(reason, result.message)
            total_ms += probe
            after = measure()
            if after.state not in _LOCKED_STATES or after.x is None or after.y is None:
                return failed("target_not_found_guide")
            moved = (after.x - before.x, after.y - before.y)
            if math.hypot(*moved) >= cfg.min_displacement_px:
                break
        magnitude = math.hypot(*moved)
        if magnitude < cfg.min_displacement_px:
            return failed(
                "guide_pulse_calibration_failed",
                f"{total_ms} ms of {axis.name} {direction.name} guide pulses moved the guide star "
                f"only {magnitude:.1f} px (< {cfg.min_displacement_px:g} px) -- the guide rate is "
                "too low to calibrate on this camera",
            )
        responses[(axis, direction)] = AxisResponse(
            axis=axis,
            direction=direction,
            duration_ms=total_ms,
            dx_px=moved[0],
            dy_px=moved[1],
            px_per_ms=magnitude / total_ms,
        )
    matrix = CalibrationMatrix(responses=responses)
    for direction in AxisDirection:
        if is_degenerate(
            matrix.response_for(MountAxis.AXIS1, direction),
            matrix.response_for(MountAxis.AXIS2, direction),
        ):
            return failed(
                "guide_pulse_calibration_failed",
                "the RA and Dec guide-pulse responses are (nearly) parallel -- cannot solve a 2-D "
                "correction from them",
            )
    return GuidePulseCalibrationOutcome(matrix, None, "", tuple(sorted(warnings)), pulses)
