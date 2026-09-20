"""Calibration move sizing (issue #46): a pure, unit-testable policy.

Mount calibration must command a move that shifts the image by roughly 25% of
the relevant frame dimension -- a minimal fixed pulse can produce a shift too
small to measure, which then looks like an estimator or stability failure. This
module decides HOW LONG to move; the panel executes it and feeds back the
MEASURED displacement, which is authoritative (nominal rate arithmetic is only
a seed: focus/finite distance/tracking all change the real image scale).

    seed from the SMALLEST participating FOV (least angular travel that still
    moves every camera), size for 25% of that camera's frame width
        -> move -> measure -> in the 20-30% band? accept
                              else rescale by target/measured and retry (bounded)
    a wider camera that saw < 10% gets its own larger follow-up move (capped)
    without touching the narrower cameras' already-good results.

Durations are bounded: never longer than `max_duration_ms` (no silent
extension -- an unreachable target is an explicit outcome) and never shorter
than `min_duration_ms` (timing/acceleration would dominate).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

#: Sidereal rate, arcsec per second.
SIDEREAL_ARCSEC_PER_S = 15.041

#: OnStep `:R#` slew-rate presets as multiples of sidereal (probed on the
#: real driver, see indi_mount_pulse_adapter). "8"/"9" (Half-Max/Max) have no
#: fixed multiple and are deliberately absent.
RATE_PRESET_X: dict[str, float] = {
    "0": 0.25,
    "1": 0.5,
    "2": 1.0,
    "3": 2.0,
    "4": 4.0,
    "5": 8.0,
    "6": 20.0,
    "7": 48.0,
}

#: A measured shift beyond this fraction of the frame is near the estimator's
#: alias limit (half the frame) and is never accepted as-is.
_ALIAS_FRACTION = 0.45
#: Per-retry scale limits, so one noisy measurement cannot swing the move wildly.
_MIN_SCALE = 0.25
_MAX_SCALE = 4.0


def rate_arcsec_per_s(preset: str) -> float:
    return RATE_PRESET_X[preset] * SIDEREAL_ARCSEC_PER_S


def finite_distance_scale(distance_m: float | None, focal_length_mm: float | None) -> float:
    """Factor applied to a camera's FOV when focused at a finite distance.

    Thin-lens: f' = f*d/(d-f), so FOV' / FOV = f/f' = 1 - f/d. Only a SEED for
    the first move (the SCT's real effective focal length depends on its focus
    position) -- measured displacement stays authoritative. No distance (stars)
    or an unknown focal length applies no correction.
    """
    if distance_m is None or focal_length_mm is None or distance_m <= 0:
        return 1.0
    return 1.0 - (focal_length_mm / 1000.0) / distance_m


@dataclass(frozen=True)
class CameraGeometry:
    """What sizing needs to know about one participating camera."""

    key: str
    width_px: int
    height_px: int
    #: Plate scale; None when the optics are not configured.
    arcsec_per_px: float | None
    focal_length_mm: float | None = None

    def fov_arcsec(self, distance_m: float | None = None) -> tuple[float, float] | None:
        if self.arcsec_per_px is None or self.arcsec_per_px <= 0:
            return None
        scale = finite_distance_scale(distance_m, self.focal_length_mm)
        return (
            self.width_px * self.arcsec_per_px * scale,
            self.height_px * self.arcsec_per_px * scale,
        )


@dataclass(frozen=True)
class SizingPolicy:
    rate_preset: str = "6"  # 20x sidereal
    target_fraction: float = 0.25
    band_low: float = 0.20
    band_high: float = 0.30
    #: A camera must see at least this fraction of its frame to be measurable.
    wide_min_fraction: float = 0.10
    max_duration_ms: int = 3000
    min_duration_ms: int = 200
    max_attempts: int = 4


class SizingStatus(Enum):
    OK = "ok"
    #: More than the cap was needed; the move runs at the cap (never longer).
    CAPPED = "capped"
    #: Less than the minimum useful duration was needed; raised to the minimum.
    RAISED_TO_MIN = "raised_to_min"
    #: No configured optics: the caller's fallback seed is used.
    NO_OPTICS = "no_optics"


@dataclass(frozen=True)
class MovePlan:
    duration_ms: int
    seed_camera: str | None
    status: SizingStatus


class StepAction(Enum):
    ACCEPT = "accept"
    RETRY = "retry"
    BOUNDED = "bounded"


@dataclass(frozen=True)
class StepDecision:
    action: StepAction
    duration_ms: int
    reason: str | None = None


def _clamp_duration(ms: float, policy: SizingPolicy) -> int:
    return int(round(max(policy.min_duration_ms, min(policy.max_duration_ms, ms))))


def plan_first_move(
    cameras: list[CameraGeometry],
    policy: SizingPolicy | None = None,
    *,
    distance_m: float | None = None,
    fallback_ms: int = 1000,
) -> MovePlan:
    """Size the first shared move from the smallest participating FOV.

    The target is 25% of that camera's frame WIDTH (the issue's own seeds): a
    camera normally sits with the mount's dominant axis along the wider side.
    The response's real dominant axis is only known after the first
    measurement (camera/FOV rotation is arbitrary), and adaptive resizing
    then corrects a vertical or rotated response via `measured_fraction`.
    """
    policy = policy or SizingPolicy()
    sized = [
        (fov[0], camera.key)
        for camera in cameras
        if (fov := camera.fov_arcsec(distance_m)) is not None
    ]
    if not sized:
        return MovePlan(fallback_ms, None, SizingStatus.NO_OPTICS)
    smallest_arcsec, key = min(sized)
    needed_ms = policy.target_fraction * smallest_arcsec / rate_arcsec_per_s(policy.rate_preset)
    needed_ms *= 1000.0
    duration = _clamp_duration(needed_ms, policy)
    if needed_ms > policy.max_duration_ms:
        status = SizingStatus.CAPPED
    elif needed_ms < policy.min_duration_ms:
        status = SizingStatus.RAISED_TO_MIN
    else:
        status = SizingStatus.OK
    return MovePlan(duration, key, status)


def measured_fraction(camera: CameraGeometry, dx_px: float, dy_px: float) -> float:
    """How far a measured shift travelled, as a fraction of the relevant frame
    dimension: the dominant component decides (horizontal -> width, vertical ->
    height), so an arbitrarily rotated camera is judged on its real 2-D vector."""
    return max(abs(dx_px) / camera.width_px, abs(dy_px) / camera.height_px)


def decide_next(
    current_ms: int, fraction: float, policy: SizingPolicy, *, attempt: int
) -> StepDecision:
    """Judge one measured move (`attempt` is 1-based) of `current_ms`.

    Measured pixels are authoritative: inside the band -> accept; otherwise
    rescale by target/measured (bounded per step) and retry, within the
    duration window and a bounded attempt count.
    """
    if policy.band_low <= fraction <= policy.band_high:
        return StepDecision(StepAction.ACCEPT, current_ms)
    scale = policy.target_fraction / max(fraction, 1e-6)
    scale = max(_MIN_SCALE, min(_MAX_SCALE, scale))
    new_ms = _clamp_duration(current_ms * scale, policy)
    overshoot = fraction > policy.band_high
    if new_ms == current_ms:  # already at a bound of the window
        if overshoot:
            return StepDecision(StepAction.BOUNDED, current_ms, "overshoot_at_minimum")
        if fraction >= policy.wide_min_fraction:
            return StepDecision(StepAction.ACCEPT, current_ms, "at_cap_below_band")
        return StepDecision(StepAction.BOUNDED, current_ms, "exceeds_envelope")
    if attempt >= policy.max_attempts:
        if policy.wide_min_fraction <= fraction <= _ALIAS_FRACTION:
            return StepDecision(StepAction.ACCEPT, current_ms, "attempts_exhausted")
        return StepDecision(StepAction.BOUNDED, current_ms, "attempts_exhausted")
    return StepDecision(StepAction.RETRY, new_ms)


def plan_followup(current_ms: int, fraction: float, policy: SizingPolicy) -> StepDecision:
    """A wider camera that saw `fraction` of its frame from a `current_ms` move.

    >= 10% is already measurable: no second move. Otherwise a larger move
    aimed at 25% of ITS frame, capped at the duration limit -- if even the cap
    cannot reach 10% the outcome is explicit (`exceeds_envelope`), never a
    silently longer move.
    """
    if fraction >= policy.wide_min_fraction:
        return StepDecision(StepAction.ACCEPT, current_ms)
    needed_ms = current_ms * policy.target_fraction / max(fraction, 1e-6)
    new_ms = _clamp_duration(needed_ms, policy)
    achieved = fraction * new_ms / current_ms
    if achieved < policy.wide_min_fraction:
        return StepDecision(StepAction.BOUNDED, current_ms, "exceeds_envelope")
    return StepDecision(StepAction.RETRY, new_ms)
