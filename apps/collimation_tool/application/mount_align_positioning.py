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
from astrotool_core.mount.movement_sizing import SIDEREAL_ARCSEC_PER_S, CameraGeometry

from collimation_tool.application.angular_move_plan import plan_angular_move
from collimation_tool.application.mount_align_failures import MeasurementFailureClass, is_no_motion
from collimation_tool.application.mount_align_guide import (
    guide_seed_rate_arcsec_per_s,
    seed_guide_ms,
    seed_note,
    unit_mismatch,
)
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
from collimation_tool.application.mount_motion import MountAlignPrimitive

#: S6.8d (R9): an axis move is an angular move or nothing -- the timed move is gone.
NO_ANGULAR_MOVE = (
    "this mount reports no angular moves (capabilities().supports_angular_moves is False) -- "
    "nothing was sent"
)


#: S6.8d: an uncalibrated nudge's size when the clicked camera has no configured optics -- a
#: fixed angle, numerically the former default (`pulse_ms` 1000 ms at the nudge `rate_preset`
#: "7", 48 x sidereal), so removing those settings changed nothing here.
NO_OPTICS_NUDGE_ARCSEC = 1000 * (48.0 * SIDEREAL_ARCSEC_PER_S) / 1000.0


# ---- S6.0: manual moves are angular ----------------------------------------------------------
def nudge_cap_arcsec(settings: MountAlignmentSettings) -> float:
    """The farthest one nudge/screen step may move (F11, S6.8d): the explicit angle
    `max_nudge_arcsec` -- its single owner is the settings; no app-side rate model."""
    return settings.max_nudge_arcsec


def guide_nudge_cap_ms(
    settings: MountAlignmentSettings,
    response: AxisResponse | None = None,
    arcsec_per_px: float | None = None,
) -> int:
    """S6.8b: the guide-pulse nudge/screen-step cap -- the same angular envelope as
    `nudge_cap_arcsec`, in guide-ms (a safety bound, not a size). Converted at the MEASURED
    guide rate of this direction (`response` px/ms x the camera's `arcsec_per_px`) once a
    calibration exists and the optics are known; else at the assumed seed rate (review #4)."""
    rate_arcsec_per_s = guide_seed_rate_arcsec_per_s(settings)
    if (
        response is not None
        and arcsec_per_px is not None
        and arcsec_per_px > 0
        and response.duration_ms > 0
        and response.magnitude_px > 0
    ):
        rate_arcsec_per_s = response.magnitude_px / response.duration_ms * arcsec_per_px * 1000.0
    return round(nudge_cap_arcsec(settings) / rate_arcsec_per_s * 1000.0)


def cap_text(settings: MountAlignmentSettings, *, guide_pulse: bool = False) -> str:
    """The nudge/screen-step safety cap for a message: always an angle (S6.8d: no timed move)."""
    if guide_pulse:  # S6.8b: the cap is an angle, whatever guide rate converts it
        return f'{nudge_cap_arcsec(settings):.0f}" of guide pulses'
    return f'{nudge_cap_arcsec(settings):.0f}"'


def uncalibrated_nudge_arcsec(
    settings: MountAlignmentSettings,
    camera_key: str,
    cameras: Callable[[], list[CameraGeometry]] | None,
    distance_m: Callable[[], float | None],
) -> float:
    """AGENTS.md / #31 Phase A: a pre-calibration nudge specified in frame terms
    (`nudge_target_fraction` of the clicked camera's frame width), converted to an angle from
    that camera's configured optics (`cameras`, the geometry provider; None = none). Without optics:
    the fixed `NO_OPTICS_NUDGE_ARCSEC`."""
    if cameras is not None:
        distance = distance_m()
        for camera in cameras():
            fov = camera.fov_arcsec(distance) if camera.key == camera_key else None
            if fov is not None:
                return settings.nudge_target_fraction * fov[0]
    return NO_OPTICS_NUDGE_ARCSEC


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
    primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
    arcsec_per_px: Callable[[], float | None] = lambda: None,
) -> NudgePlan:
    """Size one RA/Dec nudge.

    Uncalibrated (no `matrix`): `uncalibrated_arcsec` (sized from the camera's optics) -- RA/Dec
    buttons are never held hostage to a calibration existing (real diagnostic 5e71b958).
    Calibrated: the clicked direction's own measured rate (#31 Phase C) solves the duration that
    moves `nudge_target_fraction` of THIS camera's frame width (`peek_frame`).

    Real reports de295656/6cb859d2: a solved move beyond the safety cap is CLAMPED to the cap
    (a real, smaller step; clicking again continues), never refused. S6.0: the nudge is an
    angular move, and one below the mount's smallest angular move is refused before anything is
    sent. S6.8d: a mount without angular moves gets an explicit refusal (no timed move exists).

    S6.8b: with `primitive` GUIDE_PULSE the nudge is guide-pulse ms -- uncalibrated: the
    optics-sized angle at the ASSUMED guide-rate seed (labelled `seed_note`); calibrated:
    from a guide-pulse matrix only. A matrix measured with the other primitive is refused
    (`unit_mismatch`), nothing sent."""
    axis_label = AXIS_LABELS[axis]
    if matrix is not None and (mismatch := unit_mismatch(matrix.unit, primitive)):
        return NudgePlan(error=mismatch)
    guide = primitive is MountAlignPrimitive.GUIDE_PULSE
    if guide and matrix is None:
        duration_ms = seed_guide_ms(uncalibrated_arcsec(), settings)
        cap_ms = guide_nudge_cap_ms(settings)
        return NudgePlan(
            move=NudgeMove(
                camera_key, axis, direction, min(duration_ms, cap_ms), duration_ms > cap_ms,
                seed_note=seed_note(settings),
            )
        )
    if not guide and not angular_only():
        return NudgePlan(error=NO_ANGULAR_MOVE)
    duration_ms = 0
    if matrix is not None:
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
    if guide:
        cap_ms = guide_nudge_cap_ms(
            settings, matrix.response_for(axis, direction) if matrix else None, arcsec_per_px()
        )
        clamped = duration_ms > cap_ms
        return NudgePlan(
            move=NudgeMove(camera_key, axis, direction, min(duration_ms, cap_ms), clamped)
        )
    # S6.0: the nudge is an angular move. Uncalibrated: sized from the camera's optics;
    # calibrated: the solved duration is in the calibration's own unit (see
    # `angular_unit_rate`), so it converts back to the exact angle.
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
    return NudgePlan(move=NudgeMove(camera_key, axis, direction, duration_ms, clamped, arcsec))


@dataclass(frozen=True)
class ScreenMovePlan:
    """A solved screen move (`motion`), or why none is sent: `error` (a refusal) / `notice`."""

    motion: list[MotionStep] | None = None
    clamped: bool = False
    #: The axis component dropped below the mount's smallest move ("" = none).
    skipped: str = ""
    #: True = an angular move (S6.0); False for guide pulses.
    angular: bool = False
    error: str | None = None
    notice: str | None = None
    #: S6.6b: `error` is the mount's smallest angular move (it can never be made at this size).
    below_floor: bool = False


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
    primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
    guide_min_ms: int = 1,
    arcsec_per_px: float | None = None,
) -> ScreenMovePlan:
    """Issue #31 Phase D: the (possibly two-axis) sequence that moves this camera's image the
    requested on-screen direction by `fraction` of its own frame. Clamped rather than refused
    beyond the nudge safety cap (real report de295656, same setting as a nudge). S6.8b: with
    `primitive` GUIDE_PULSE the steps are guide-pulse ms from a guide-pulse matrix (a
    matrix of the other primitive is refused, nothing sent), capped per step."""
    if (mismatch := unit_mismatch(matrix.unit, primitive)) is not None:
        return ScreenMovePlan(error=mismatch)
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
    if primitive is MountAlignPrimitive.GUIDE_PULSE:
        # A component below the shortest guide pulse (`guide_min_ms`, a fraction of an arcsec)
        # is dropped and said so -- never sent, never a partially executed sequence.
        kept = [step for step in steps if step[2] >= guide_min_ms]
        if not kept:
            return ScreenMovePlan(notice=f"{CAMERA_LABELS[camera_key]}: nothing to move.")
        caps = [
            guide_nudge_cap_ms(settings, matrix.response_for(axis, direction), arcsec_per_px)
            for axis, direction, _ms in kept
        ]
        guided: list[MotionStep] = [
            (axis, direction, min(duration_ms, cap))
            for (axis, direction, duration_ms), cap in zip(kept, caps, strict=True)
        ]
        skipped = ", ".join(
            f"{AXIS_LABELS[axis]} {ms} guide-ms" for axis, _d, ms in steps if ms < guide_min_ms
        )
        return ScreenMovePlan(
            motion=guided,
            clamped=any(step[2] > cap for step, cap in zip(kept, caps, strict=True)),
            skipped=skipped,
        )
    if not angular_only():
        return ScreenMovePlan(error=NO_ANGULAR_MOVE)
    planned = plan_angular_screen_move(
        settings, matrix, steps, math.hypot(target_dx_px, target_dy_px), floor_arcsec()
    )
    if isinstance(planned, str):
        # Refused BEFORE anything is sent -- never a partially executed sequence.
        return ScreenMovePlan(error=planned, below_floor=True)
    motion, clamped, skipped = planned
    return ScreenMovePlan(motion=motion, clamped=clamped, skipped=skipped, angular=True)


def screen_move_floor_refusals(
    *,
    settings: MountAlignmentSettings,
    matrices: Mapping[str, CalibrationMatrix],
    frame_shapes: Mapping[str, tuple[int, int]],
    directions: list[ScreenDirection],
    fraction: float,
    size_label: str,
    mode: TargetMode,
    angular_only: bool,
    floor_arcsec: float,
) -> dict[str, dict[ScreenDirection, str]]:
    """S6.6b (S6.0 re-review b): per camera, the screen directions the mount's smallest angular
    move (`MountCapabilities.min_angular_arcsec`) forbids at this size -> the reason. Decided by
    `plan_screen_move` itself (`below_floor`), so a disabled button and a refused click always
    agree; e.g. Small up/down on the real main camera (5% of 440" = 22" < 30")."""
    refusals: dict[str, dict[ScreenDirection, str]] = {}
    if not angular_only or floor_arcsec <= 0:
        return refusals
    for key, matrix in matrices.items():
        shape = frame_shapes.get(key)
        if shape is None:
            continue
        frame = np.zeros(shape, dtype=np.float32)
        refused: dict[ScreenDirection, str] = {}
        for direction in directions:
            plan = plan_screen_move(
                settings=settings,
                camera_key=key,
                matrix=matrix,
                screen_direction=direction,
                fraction=fraction,
                frame=frame,
                mode=mode,
                angular_only=lambda: True,
                floor_arcsec=lambda: floor_arcsec,
            )
            if plan.below_floor and plan.error is not None:
                refused[direction] = f"{size_label} is too small for this mount: {plan.error}"
        refusals[key] = refused
    return refusals


def nudge_tracking_warning(reason: str) -> str:
    """S6.6b (review C2): the nudge moved, but its no-motion check could not run because of the
    mount's tracking state (#44) -- shown and recorded as a warning, in nudge wording."""
    return f"WARNING: the no-motion check after this nudge could not run -- {reason}"


#: S6.6b: why a nudge's background no-motion check could not run.
NO_REFERENCE_NOTE = (
    " (no-motion check skipped: no verified at-rest view before this move -- the next nudge "
    "is checked)"
)


@dataclass(frozen=True)
class NudgeCheck:
    """What the background #43 check after a nudge found (S6.6b): the confirmed responses, the
    per-camera failure classes, a `warning` ONLY when a camera measured no image motion, and
    the `note` appended to the nudge's "done" text."""

    responses: dict[str, AxisResponse]
    failure_classes: dict[str, MeasurementFailureClass]
    warning: str | None
    note: str


def check_nudge_motion(
    before: Mapping[str, Measurement],
    after: Mapping[str, Measurement] | None,
    build: Callable[[Measurement, Measurement], AxisResponse | None],
    *,
    missing: Callable[[], str],
) -> NudgeCheck:
    """USER DECISION (2026-10-10): the nudge itself has already returned -- the mount is
    wherever it physically ended up. This check compares the verified at-rest BEFORE with fresh,
    stability-verified AFTER frames and only ADDS a warning when a camera with a confident
    match saw NO image motion (issue #43: never a confirmed displacement). A camera that cannot
    confirm the move (no frame, no match) never blocks another camera's reading and is not a
    warning. `before` empty = no valid reference existed -> nothing to compare (said so)."""
    if not after:  # first: it also tells why the next nudge will have no reference
        return NudgeCheck({}, {}, None, f" (no-motion check not possible: {missing()})")
    if not before:
        return NudgeCheck({}, {}, None, NO_REFERENCE_NOTE)
    responses: dict[str, AxisResponse] = {}
    failure_classes: dict[str, MeasurementFailureClass] = {}
    still: list[str] = []
    parts: list[str] = []
    for key in ("left", "right"):
        if key not in before or key not in after:
            # Issue #30: no usable frame (already classified by the capture) -- not a warning.
            parts.append(f"{CAMERA_LABELS[key]}: displacement not confirmed")
            continue
        response = build(before[key], after[key])
        if response is None:
            failure_classes[key] = MeasurementFailureClass.MATCH_FAILED
            parts.append(f"{CAMERA_LABELS[key]}: displacement not confirmed")
        elif is_no_motion(response, before[key]):
            failure_classes[key] = MeasurementFailureClass.NO_MOTION_DETECTED
            still.append(CAMERA_LABELS[key])
        else:
            responses[key] = response
            parts.append(f"{CAMERA_LABELS[key]}: {format_response(response)}")
    warning = (
        f"WARNING: no image motion measured on {', '.join(still)} after the move -- check that "
        "the mount really moves (unparked, motion enabled, clutch engaged)"
        if still
        else None
    )
    shown = ([warning] if warning else []) + parts
    return NudgeCheck(responses, failure_classes, warning, f" {' | '.join(shown)}" if shown else "")
