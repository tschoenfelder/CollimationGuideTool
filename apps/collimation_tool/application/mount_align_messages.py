"""Mount Align user-facing text (S6.6a, moved verbatim out of the panel).

The wording of every capture/stability failure note, the per-camera/axis labels, the degenerate
calibration explanation and the response formatters. Pure functions over values -- no Qt, no
device access. The panel and the Mount Align application modules render through these.
"""

from __future__ import annotations

from collections.abc import Mapping

from astrotool_core.acquisition import (
    FrameAcquisitionStatus,
    MotionAwareFrameResult,
    MotionAwareStatus,
    StabilityStatus,
)
from astrotool_core.mount import AxisResponse, MountAxis

from collimation_tool.application.mount_align_failures import MeasurementFailureClass
from collimation_tool.application.mount_align_measurement import TargetMode

#: Issue #27/#30: this panel composes `astrotool_core.acquisition.
#: motion_aware_acquisition.acquire_verified_frames` over its own two named
#: sources ("left"/"right") -- `wait_for_left_frame`/`wait_for_right_frame`
#: (constructor params, normally `CameraPanel.wait_for_frame_after` bound
#: per camera) share issue #27's own `StableFrameWaiter` contract:
#: `(reference_monotonic, timeout_s) -> FrameAcquisitionResult`, whose
#: `.status` distinguishes *why* a wait failed instead of collapsing every
#: cause into a bare `None` -- the underlying exposure-timing half
#: `acquire_verified_frames` itself waits on before ever reaching its own
#: stability check.
STATUS_MESSAGES: dict[FrameAcquisitionStatus, str] = {
    FrameAcquisitionStatus.TIMEOUT: "timed out waiting for a frame",
    FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION: (
        "every frame's own exposure overlapped the move -- try a shorter camera "
        "exposure or a longer calibration pulse"
    ),
    FrameAcquisitionStatus.CAMERA_UNAVAILABLE: "camera not available",
    FrameAcquisitionStatus.SETTLE_NOT_REACHED: "caught up, but no frame after the settle wait",
    FrameAcquisitionStatus.CANCELLED: "cancelled",
}
#: Field bundle 1b157b8b: `EXPOSURE_OVERLAPPED_MOTION`'s own message above
#: says "...overlapped the move", but this same status is also reached by
#: a nudge/calibration sequence's very first "before" capture, which runs
#: with no prior pulse at all (`self._last_movement_context is None`) --
#: there is no move to overlap. Used instead of the message above
#: whenever `_capture_failure_detail()` knows no movement was commanded
#: yet for this capture.
EXPOSURE_OVERLAPPED_NO_MOVE_MESSAGE = (
    "no frame's exposure could be confirmed to start after the request -- "
    "try a shorter camera exposure"
)

#: Issue #30: message text for a capture that got frames (unlike anything
#: in `STATUS_MESSAGES` above) but couldn't verify them stable --
#: distinct wording so the two failure classes read differently, not just
#: differ in their `MeasurementFailureClass` value. Keyed by
#: `MotionAwareStatus`, never includes `OK`/`CAPTURE_INVALID` (the latter
#: is instead resolved through `STATUS_MESSAGES` via
#: `MotionAwareFrameResult.diagnostics["capture_status"]` -- see
#: `_capture_failure_detail()`).
MOTION_STATUS_MESSAGES: dict[MotionAwareStatus, str] = {
    MotionAwareStatus.IMAGE_NOT_STABLE: (
        "image kept moving beyond tolerance -- possible wind/vibration, not a "
        "measurement-algorithm problem"
    ),
    MotionAwareStatus.SETTLE_TIMEOUT: "timed out before a full stability check could even run",
    MotionAwareStatus.CANCELLED: "cancelled",
}
#: Field bundle 1b157b8b: `IMAGE_NOT_STABLE`'s own generic wording above
#: assumes real measured displacement (`StabilityStatus.UNSTABLE`), but the
#: same outer status is also reached when a frame pair had NO correlatable
#: content at all (`StabilityStatus.INDETERMINATE` -- `max_displacement_px`
#: stays `None`, nothing was ever actually measured, most likely an
#: over-saturated/blurred/low-contrast frame, not motion). Used instead of
#: `MOTION_STATUS_MESSAGES[IMAGE_NOT_STABLE]` whenever
#: `result.stability.status is StabilityStatus.INDETERMINATE`.
STABILITY_INDETERMINATE_MESSAGE = (
    "frames could not be compared for stability (correlation failed -- possibly "
    "over-saturated, low-contrast, or blurred), not necessarily real motion -- "
    "try lowering exposure/gain"
)


CAMERA_LABELS = {"left": "Main", "right": "Guide"}
AXIS_LABELS = {MountAxis.AXIS1: "RA-axis", MountAxis.AXIS2: "Dec-axis"}
OTHER_CAMERA = {"left": "right", "right": "left"}


def format_response(response: AxisResponse) -> str:
    return (
        f"dx={response.dx_px:+.1f}px dy={response.dy_px:+.1f}px "
        f"({response.magnitude_px:.1f}px @ {response.angle_degrees:.0f}°)"
    )


def degenerate_calibration_message(
    camera_key: str,
    axis1: AxisResponse,
    axis2: AxisResponse,
    other_camera_responses: dict[MountAxis, AxisResponse],
) -> str:
    """Real report (diagnostic 0270868c): AXIS2 measured zero on Main but
    a large real shift on Guide from the *same* pulse -- Main's much
    finer plate scale had likely panned that same real motion entirely
    out of frame overlap, not a mount/hardware problem. Guide's own
    reading for whichever axis measured exactly zero here tells the
    difference, without needing to estimate a replacement vector for the
    degenerate camera (deliberately not attempted -- the two cameras
    aren't guaranteed to share the same rotational alignment to the
    mount axes, so a scale-only estimate could get the *direction*
    wrong; see compose_screen_move's own docstring for why that
    rotation is solved per-camera in the first place): zero on both
    cameras is a real, actionable "check the mount" signal; zero here
    but real motion confirmed on the other camera points at this
    camera's own framing/plate-scale instead.
    """
    zero_axes = [
        axis
        for axis, response in ((MountAxis.AXIS1, axis1), (MountAxis.AXIS2, axis2))
        if response.magnitude_px == 0.0
    ]
    other_label = CAMERA_LABELS[OTHER_CAMERA[camera_key]]
    notes: list[str] = []
    for axis in zero_axes:
        other = other_camera_responses.get(axis)
        if other is not None and other.magnitude_px > 0.0:
            notes.append(
                f"{AXIS_LABELS[axis]} measured no motion here, but {other_label} confirms "
                "real motion on it -- likely this camera's own framing/plate scale, not a "
                "mount issue."
            )
        else:
            notes.append(
                f"{AXIS_LABELS[axis]} measured no motion on either camera -- may be a "
                "real mount/cable issue; check the mount directly."
            )
    detail = (
        " ".join(notes)
        if notes
        else (
            "one axis may not have moved anything real even though the pulse was accepted; "
            "check the mount directly, or try Run Calibration again."
        )
    )
    return f"{CAMERA_LABELS[camera_key]}: RA-axis and Dec-axis too close to parallel -- {detail}"


def response_dict(response: AxisResponse) -> dict[str, float]:
    return {
        "dx_px": response.dx_px,
        "dy_px": response.dy_px,
        "magnitude_px": response.magnitude_px,
        "angle_degrees": response.angle_degrees,
    }


def missing_label(mode: TargetMode) -> str:
    return "no star detected" if mode == "star" else "no frame available"


def describe_capture_failures(
    failures: Mapping[str, MotionAwareFrameResult], *, had_movement_context: bool
) -> str:
    """A short, status-specific note appended to a generic capture-failure message -- e.g.
    `" (Main: every frame's own exposure overlapped the move...)"` -- one entry per camera that
    failed, and why. Issue #27: failures from frame acquisition are distinguishable; issue #30
    adds the stability layer's own wording. Empty string if nothing informative is known.

    Field bundle 1b157b8b: `EXPOSURE_OVERLAPPED_MOTION` with no commanded movement yet
    (`had_movement_context` False) gets its no-move wording, and `IMAGE_NOT_STABLE` with
    nothing actually measured (`StabilityStatus.INDETERMINATE`) gets the correlation-failed
    wording instead of blaming wind/vibration."""
    if not failures:
        return ""
    parts = []
    for key, result in failures.items():
        if result.status is MotionAwareStatus.CAPTURE_INVALID:
            capture_status = FrameAcquisitionStatus(result.diagnostics["capture_status"])
            if (
                capture_status is FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION
                and not had_movement_context
            ):
                message = EXPOSURE_OVERLAPPED_NO_MOVE_MESSAGE
            else:
                message = STATUS_MESSAGES[capture_status]
        elif (
            result.status is MotionAwareStatus.IMAGE_NOT_STABLE
            and result.stability is not None
            and result.stability.status is StabilityStatus.INDETERMINATE
        ):
            message = STABILITY_INDETERMINATE_MESSAGE
        else:
            message = MOTION_STATUS_MESSAGES[result.status]
        parts.append(f"{CAMERA_LABELS[key]}: {message}")
    return " (" + "; ".join(parts) + ")"


def no_motion_text(size: str, *, angular: bool) -> str:
    """Issue #43: why a camera was excluded when its image did not move. `size` is the commanded
    move in its own unit (`CalibrationSizing.format_size`). S6.8d: no rate preset exists, so none
    is named (a guide-pulse run used to read "... ms move at preset None")."""
    if angular:
        return (
            f"stable frames and a confident match, but the image did not move for a commanded "
            f"{size} angular move -- check that the mount really moves (unparked, "
            "motion enabled, tracking off)"
        )
    return (
        f"stable frames and a confident match, but the image did not move for a commanded "
        f"{size} move -- check that the mount really moves (unparked, tracking on, "
        "guide pulses enabled)"
    )


def bounded_text(failure: MeasurementFailureClass, *, largest_move: str) -> str:
    """Issue #46: why a camera was excluded when sizing could not bring its move into band.
    `largest_move`: the largest normal calibration move in the run's own unit (an angle, or
    guide-ms). S6.8d: the advice names the optics -- the rate presets it used to suggest
    (`calibration_rate_preset`) are gone."""
    if failure is MeasurementFailureClass.MOVE_EXCEEDS_ENVELOPE:
        return (
            f"even the largest normal calibration move ({largest_move}) shifts this camera "
            "by less than a measurable part of its frame (exceeds the calibration "
            "envelope) -- check this camera's configured optics (plate scale)"
        )
    return (
        "the smallest calibration move the mount accepts already overshoots this "
        "camera's frame -- check this camera's configured optics (plate scale)"
    )


def calibration_progress_text(
    axis: MountAxis, direction_name: str, role_value: str, duration_ms: int, arcsec: float | None
) -> str:
    """The status line while one measured calibration step runs."""
    return (
        f"Calibrating {axis.name} ({direction_name.lower()}, "
        f"{role_value}, "
        + (f"{arcsec:.0f} arcsec angular" if arcsec is not None else f"{duration_ms} ms")
        + ")…"
    )


def partial_move_text(
    motion_axes: tuple[MountAxis, ...], attempted: int, error: str
) -> str | None:
    """S6.6b: a multi-axis move that stopped after an earlier axis had already moved -- say
    which axis moved and which was refused ("RA moved; Dec refused: ..."), so "Move failed" is
    not read as "nothing moved". None when no earlier axis moved."""
    if attempted < 2 or attempted > len(motion_axes):
        return None
    short = {MountAxis.AXIS1: "RA", MountAxis.AXIS2: "Dec"}
    moved = ", ".join(short[axis] for axis in motion_axes[: attempted - 1])
    return f"{moved} moved; {short[motion_axes[attempted - 1]]} refused: {error}"
