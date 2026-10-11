"""Mount Align manual positioning: sizing a RA/Dec nudge and a screen-relative move (S6.6a,
moved out of the panel unchanged; #31 Phase A/D, S6.0).

Both are decided completely BEFORE anything is sent: the result is either a move to submit, a
refusal (`error`, shown as "Move failed: ...") or a notice (nothing to move). No Qt, no device
access -- frames and optics are read through the callables the caller passes.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import (
    AxisDirection,
    AxisResponse,
    CalibrationMatrix,
    MountAxis,
    solve_screen_move,
)
from astrotool_core.mount.movement_sizing import RATE_PRESET_X, CameraGeometry, rate_arcsec_per_s

from collimation_tool.application.angular_move_plan import plan_angular_move
from collimation_tool.application.mount_align_failures import MeasurementFailureClass, is_no_motion
from collimation_tool.application.mount_align_measurement import Measurement, TargetMode
from collimation_tool.application.mount_align_messages import (
    AXIS_LABELS,
    CAMERA_LABELS,
    format_response,
    missing_label,
)
from collimation_tool.application.mount_align_motion import MotionStep
from collimation_tool.application.mount_align_plan import NudgeMove, ScreenDirection
from collimation_tool.application.mount_align_sizing import angular_unit_rate


# ---- S6.0: manual moves on a mount without timed pulses --------------------------------------
def nudge_rate_arcsec_per_s(settings: MountAlignmentSettings) -> float:
    preset = settings.rate_preset
    if preset in RATE_PRESET_X:
        return rate_arcsec_per_s(preset)
    return angular_unit_rate(settings)


def nudge_cap_arcsec(settings: MountAlignmentSettings) -> float:
    """The farthest one nudge/screen step may move on an angular-only mount: the same envelope a
    timed nudge had (`max_nudge_pulse_ms` at the nudge `rate_preset`)."""
    return settings.max_nudge_pulse_ms * nudge_rate_arcsec_per_s(settings) / 1000.0


def cap_text(settings: MountAlignmentSettings, angular: bool) -> str:
    if angular:
        return f'{nudge_cap_arcsec(settings):.0f}"'
    return f"{settings.max_nudge_pulse_ms}ms"


def uncalibrated_nudge_arcsec(
    settings: MountAlignmentSettings,
    camera_key: str,
    cameras: Callable[[], list[CameraGeometry]] | None,
    distance_m: Callable[[], float | None],
) -> float:
    """AGENTS.md / #31 Phase A: a pre-calibration nudge specified in frame terms
    (`nudge_target_fraction` of the clicked camera's frame width), converted to an angle from
    that camera's configured optics (`cameras`, the geometry provider; None = none). Without optics:
    the size the configured timed nudge (`pulse_ms` at `rate_preset`) used to command."""
    if cameras is not None:
        distance = distance_m()
        for camera in cameras():
            fov = camera.fov_arcsec(distance) if camera.key == camera_key else None
            if fov is not None:
                return settings.nudge_target_fraction * fov[0]
    return settings.pulse_ms * nudge_rate_arcsec_per_s(settings) / 1000.0


@dataclass(frozen=True)
class NudgePlan:
    """A sized nudge (`move`), or why none is sent: `error` (a refusal) / `notice`."""

    move: NudgeMove | None = None
    error: str | None = None
    notice: str | None = None


def plan_nudge(
    *,
    settings: MountAlignmentSettings,
    camera_key: str,
    axis: MountAxis,
    direction: AxisDirection,
    matrix: CalibrationMatrix | None,
    peek_frame: Callable[[], np.ndarray | None],
    mode: TargetMode,
    angular_only: Callable[[], bool],
    uncalibrated_arcsec: Callable[[], float],
    floor_arcsec: Callable[[], float],
) -> NudgePlan:
    """Size one RA/Dec nudge.

    Uncalibrated (no `matrix`): the fixed `pulse_ms` a calibration step itself uses -- RA/Dec
    buttons are never held hostage to a calibration existing (real diagnostic 5e71b958).
    Calibrated: the clicked direction's own measured rate (#31 Phase C) solves the duration that
    moves `nudge_target_fraction` of THIS camera's frame width (`peek_frame`).

    Real reports de295656/6cb859d2: a solved move beyond the safety cap is CLAMPED to the cap
    (a real, smaller step; clicking again continues), never refused. S6.0: on an angular-only
    mount the nudge is an angular move (uncalibrated: sized from the camera's optics), and one
    below the mount's smallest angular move is refused before anything is sent."""
    axis_label = AXIS_LABELS[axis]
    if matrix is None:
        duration_ms = settings.pulse_ms
    else:
        response = matrix.response_for(axis, direction)
        if response.magnitude_px <= 0 or response.duration_ms <= 0:
            # Shouldn't happen for a non-degenerate matrix -- defensive, so a genuinely zero
            # rate can't divide by zero below instead of reporting cleanly.
            return NudgePlan(
                error=(
                    f"{axis_label} measured no usable motion during calibration -- "
                    "try Run Calibration again."
                )
            )
        frame = peek_frame()
        if frame is None:
            return NudgePlan(error=f"{missing_label(mode)} before pulsing")
        _frame_height, frame_width = frame.shape[:2]
        target_px = settings.nudge_target_fraction * frame_width
        rate_px_per_ms = response.magnitude_px / response.duration_ms
        duration_ms = round(target_px / rate_px_per_ms)
        if duration_ms <= 0:
            return NudgePlan(notice=f"{CAMERA_LABELS[camera_key]}: nothing to move.")
    arcsec: float | None = None
    if angular_only():
        # S6.0: no timed pulse exists -- the nudge is an angular move. Uncalibrated: sized from
        # the camera's optics; calibrated: the solved duration is in the calibration's own unit
        # (see `angular_unit_rate`), so it converts back to the exact angle.
        unit = angular_unit_rate(settings)
        arcsec = uncalibrated_arcsec() if matrix is None else duration_ms * unit / 1000.0
        cap = nudge_cap_arcsec(settings)
        clamped = arcsec > cap
        arcsec = min(arcsec, cap)
        floor = floor_arcsec()
        if arcsec < floor:
            # Refused before anything is sent: the mount cannot make a move this small.
            return NudgePlan(
                error=(
                    f"{AXIS_LABELS[axis]} move of {arcsec:.1f}\" is below the mount's "
                    f'smallest angular move ({floor:.0f}") -- nothing was sent'
                )
            )
        duration_ms = max(1, round(arcsec / unit * 1000.0))
    else:
        clamped = duration_ms > settings.max_nudge_pulse_ms
        if clamped:
            duration_ms = settings.max_nudge_pulse_ms
    return NudgePlan(move=NudgeMove(camera_key, axis, direction, duration_ms, clamped, arcsec))


@dataclass(frozen=True)
class ScreenMovePlan:
    """A solved screen move (`motion`), or why none is sent: `error` (a refusal) / `notice`."""

    motion: list[MotionStep] | None = None
    clamped: bool = False
    #: The axis component dropped below the mount's smallest move ("" = none).
    skipped: str = ""
    #: True = an angular-only mount (S6.0): the safety cap is an angle.
    angular: bool = False
    error: str | None = None
    notice: str | None = None


def plan_angular_screen_move(
    settings: MountAlignmentSettings,
    matrix: CalibrationMatrix,
    steps: list[tuple[MountAxis, AxisDirection, int]],
    target_px: float,
    floor: float,
) -> tuple[list[MotionStep], bool, str] | str:
    """S6.0: the whole angular sequence for a screen move, validated BEFORE anything is sent.
    Returns (steps, clamped, skipped axis label) or a refusal message.

    The rule (cap like a nudge; a sub-floor component DROPPED only when the image error it
    leaves is within `SCREEN_MOVE_DROP_TOLERANCE` of the requested shift, else the whole move
    REFUSED with nothing sent) has one owner shared with the guide-assisted recenter policy:
    `collimation_tool.application.angular_move_plan` (S6.0b). Never a partially executed
    sequence (a refused second component used to strand the first)."""
    plan = plan_angular_move(
        matrix,
        steps,
        target_px=target_px,
        center_rate_x=settings.calibration_center_rate_x,
        cap_arcsec=nudge_cap_arcsec(settings),
        floor_arcsec=floor,
    )
    dropped = ", ".join(f'{AXIS_LABELS[axis]} {arcsec:.1f}"' for axis, arcsec in plan.dropped)
    if plan.refused:
        return (
            f"this move needs {dropped}, below the mount's smallest angular "
            f'move ({floor:.0f}") -- nothing was sent; choose a larger size'
        )
    kept: list[MotionStep] = [
        (step.axis, step.direction, step.duration_ms, step.arcsec) for step in plan.steps
    ]
    return kept, plan.clamped, dropped


def plan_screen_move(
    *,
    settings: MountAlignmentSettings,
    camera_key: str,
    matrix: CalibrationMatrix,
    screen_direction: ScreenDirection,
    fraction: float,
    frame: np.ndarray | None,
    mode: TargetMode,
    angular_only: Callable[[], bool],
    floor_arcsec: Callable[[], float],
) -> ScreenMovePlan:
    """Issue #31 Phase D: the (possibly two-axis) sequence that moves this camera's image the
    requested on-screen direction by `fraction` of its own frame. Clamped rather than refused
    beyond the nudge safety cap (real report de295656, same setting as a nudge)."""
    if frame is None:
        return ScreenMovePlan(error=f"{missing_label(mode)} before pulsing")
    frame_height, frame_width = frame.shape[:2]
    dx_sign, dy_sign = screen_direction.value
    target_dx_px = dx_sign * fraction * frame_width
    target_dy_px = dy_sign * fraction * frame_height
    try:
        steps = solve_screen_move(matrix, target_dx_px=target_dx_px, target_dy_px=target_dy_px)
    except ValueError as exc:
        return ScreenMovePlan(error=str(exc))
    if not steps:
        return ScreenMovePlan(notice=f"{CAMERA_LABELS[camera_key]}: nothing to move.")
    angular = angular_only()
    if angular:
        planned = plan_angular_screen_move(
            settings, matrix, steps, math.hypot(target_dx_px, target_dy_px), floor_arcsec()
        )
        if isinstance(planned, str):
            # Refused BEFORE anything is sent -- never a partially executed sequence.
            return ScreenMovePlan(error=planned)
        motion, clamped, skipped = planned
        return ScreenMovePlan(motion=motion, clamped=clamped, skipped=skipped, angular=True)
    clamped = any(duration_ms > settings.max_nudge_pulse_ms for _, _, duration_ms in steps)
    timed: list[MotionStep] = [
        (axis, direction, min(duration_ms, settings.max_nudge_pulse_ms))
        for axis, direction, duration_ms in steps
    ]
    return ScreenMovePlan(motion=timed, clamped=clamped)


@dataclass(frozen=True)
class NudgeOutcome:
    """What a nudge's AFTER capture confirmed, per camera, and the text to show."""

    responses: dict[str, AxisResponse]
    failure_classes: dict[str, MeasurementFailureClass]
    error: str | None
    text: str


def evaluate_nudge(
    before: Mapping[str, Measurement],
    after: Mapping[str, Measurement],
    build: Callable[[Measurement, Measurement], AxisResponse | None],
    *,
    clamped: bool,
    cap_text: Callable[[], str],
) -> NudgeOutcome:
    """Real request: the pulse already ran -- the mount is wherever it physically ended up,
    whether or not each camera could confirm *how far*. A camera that cannot confirm the move
    (no frame, no match, or -- issue #43 -- no measurable motion) never blocks another camera's
    confirmed reading and is not a move failure (mirrors calibration's per-camera handling,
    real report d14c3a9b)."""
    responses: dict[str, AxisResponse] = {}
    failure_classes: dict[str, MeasurementFailureClass] = {}
    unconfirmed: list[str] = []
    for key in ("left", "right"):
        if key not in before or key not in after:
            # Issue #30: no usable frame at all (already classified by the capture).
            unconfirmed.append(key)
            continue
        response = build(before[key], after[key])
        if response is None:
            unconfirmed.append(key)
            failure_classes[key] = MeasurementFailureClass.MATCH_FAILED
        elif is_no_motion(response, before[key]):
            unconfirmed.append(key)  # Issue #43: not a confirmed displacement
            failure_classes[key] = MeasurementFailureClass.NO_MOTION_DETECTED
        else:
            responses[key] = response
    error = (
        f"could not confirm the displacement for: {', '.join(unconfirmed)} "
        "-- the move itself still happened"
        if unconfirmed
        else None
    )
    parts = [
        f"{CAMERA_LABELS[key]}: {format_response(responses[key])}"
        if key in responses
        else f"{CAMERA_LABELS[key]}: moved, displacement not confirmed"
        for key in ("left", "right")
        if key in responses or key in unconfirmed
    ]
    suffix = (
        f" (safety-capped at {cap_text()} -- click again to continue toward the target)"
        if clamped
        else ""
    )
    return NudgeOutcome(responses, failure_classes, error, " | ".join(parts) + suffix)
