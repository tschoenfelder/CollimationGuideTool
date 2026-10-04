"""CollimationRecenterPolicy — drives a measured pixel offset to zero via
pulse-guide corrections.

Redesigned (not a literal port) from smart_telescope's
`services/collimation/mount_centering.py::PulseCenterer`. The old
implementation derived pulse duration from a theoretical sidereal-rate
constant (`pixel_scale_arcsec` + a guide-rate fraction) and called the old
MountPort's `guide(direction: str, duration_ms)` using an assumed image-
orientation sign convention (`dx>0 -> "w"`, `dy>0 -> "n"`). The new
MountPort only has `pulse_axis(axis, direction, duration_ms)`; rather than
reintroduce sidereal-rate arithmetic or an assumed orientation, this
policy uses Stage 4's empirically measured `CalibrationMatrix` directly:
`duration_ms = |offset_component_px| / measured_px_per_ms`, and picks
whichever `AxisDirection`'s calibrated response vector actually opposes
the measured error (by its measured sign, not an assumed convention).

Tolerance/settle/divergence-guard fields port near-verbatim from
`MountCenteringConfig`. One behavior change from the source (see
docs/porting-notes.md): a rejected pulse now aborts immediately instead
of being silently ignored — the original never checked `guide()`'s
returned bool.

Time (issue #53, S3b): the settle after each accepted pulse waits on the injected
`Clock` (`clock=`, default the real `SYSTEM_CLOCK`); `cancel_check` is still consulted
between iterations only, never inside a settle (nor inside a blocking angular move).

Mounts without timed pulses (S6.0b, #39 / audit P01). OnStepAdapter >= 0.4 over INDI has no
timed primitive: `OnStepMountPulseAdapter.capabilities().supports_pulse_guiding` is False and
its `pulse_axis` always refuses -- the single authoritative capability source, the same one
Mount Align's `supports_timed_pulse` reads. On such a mount every correction is an angular
`move_angular`, never a `pulse_axis`, and it is solved in 2-D from BOTH axes' measured
responses (`solve_screen_move`), so a camera rotated arbitrarily against the mount axes (or
seeing an axis inverted) converges -- review of S6.0b: mapping image x to RA and y to Dec
diverged at 90 degrees. On these mounts Mount Align stores the matrix in equivalent ms at
`center_rate_x` x sidereal (S6.0); each solved component is converted to arcsec by the one
owner of that factor (`equivalent_duration`). The whole correction is scaled (direction kept)
so no component exceeds the `max_pulse_ms` cap, then planned by the one rule shared with Mount
Align's screen moves (`angular_move_plan`): a component below the adapter-reported smallest move
(`min_angular_arcsec`) is dropped only when the error it leaves is small
(`RECENTER_DROP_TOLERANCE`), else nothing is sent and the outcome is `below_mount_minimum_move`;
the plan is validated as a whole before anything is sent. A refusal keeps the adapter's own
reason in `MountCorrectionResult.message`.

Guide pulses while tracking (S6.0d, #39): with `guide_pulses=True` every correction is a
tracking-preserving guide pulse (`GuidePulsePort.guide_pulse`, OnStepAdapter >= 0.5.0) and the
matrix is a GUIDE-PULSE calibration in real guide-pulse ms (`guide_pulse_reacquisition`), so no
rate conversion applies. The correction is solved in 2-D like the angular path; each component is
capped at the per-axis `max_step_px` share (converted through the measured matrix) and at the
adapter-reported longest pulse; a component shorter than the shortest pulse is dropped (it is
below a pixel at any practical guide rate), and nothing at all is sent when every component is.
A refused/failed pulse ends the loop as `pulse_rejected` with the adapter's message; OnStepAdapter
warnings (e.g. meridian_flip_required) and "tracking is now OFF" are carried on the result.

The timed path (mounts that report timed pulses) is unchanged and still maps image x to AXIS1
and y to AXIS2 -- rotation-blind, pinned as a known defect by a strict xfail
(test_recenter_policy.py::TestTimedPathRotation); no production timed mount is wired today.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount.axis_calibration import CalibrationMatrix, solve_screen_move
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    GuidePulseResult,
    MountAxis,
    MountPort,
)
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState
from astrotool_core.timing import SYSTEM_CLOCK, Clock

from collimation_tool.application.angular_move_plan import plan_angular_move
from collimation_tool.application.equivalent_duration import (
    arcsec_to_equivalent_ms,
    equivalent_ms_to_arcsec,
)

_LOCKED_STATES = (TrackingState.LOCKED, TrackingState.REACQUIRED)
_log = logging.getLogger(__name__)

_NO_MOTION_PRIMITIVE = (
    "this mount reports no timed pulse support (capabilities().supports_pulse_guiding is "
    "False) and offers no angular move -- nothing was sent to the mount"
)

#: S6.0b: in this closed loop a sub-floor component may be dropped when the image error it
#: leaves is at most half the current error. Unlike a fire-and-forget screen move
#: (`SCREEN_MOVE_DROP_TOLERANCE`, 25%), every correction here is re-measured before the next,
#: so dropping only has to guarantee progress (each correction at least halves the error; the
#: divergence guard and the iteration bound still apply); a stricter value would stop the loop
#: far from the reference whenever one axis' remainder is just below the mount's smallest move.
RECENTER_DROP_TOLERANCE = 0.5

_NO_GUIDE_PULSES = (
    "this mount offers no guide pulse while tracking (needs OnStepAdapter >= 0.5.0) -- "
    "nothing was sent to the mount"
)

_CorrectionStep = Callable[[], "CommandResult | GuidePulseResult"]


def _note_guide_outcome(result: CommandResult | GuidePulseResult, warnings: set[str]) -> bool:
    """S6.0d: collect a guide pulse's OnStepAdapter warnings; True when the mount was observed
    NOT tracking after it failed. Other results carry neither."""
    if not isinstance(result, GuidePulseResult):
        return False
    warnings.update(result.warnings)
    return result.tracking_off


@dataclass(frozen=True)
class _NotSent:
    """A correction that could not be planned: nothing was sent to the mount."""

    reason: str
    message: str


@dataclass(frozen=True)
class RecenterConfig:
    max_pulse_ms: int = 500
    settle_ms: int = 750
    fine_tolerance_px: float = 5.0
    rough_tolerance_px: float = 20.0
    max_iterations: int = 30
    max_diverge_count: int = 3


@dataclass(frozen=True)
class MountCorrectionResult:
    """reason is one of: within_tolerance, star_lost, diverging,
    pulse_rejected, max_pulses, cancelled, below_mount_minimum_move,
    move_cap_below_mount_minimum, calibration_degenerate.

    `pulse_rejected`: the mount refused a correction (timed pulse or angular move) --
    `message` carries the adapter's own reason. Mounts without timed pulses only, nothing sent
    in each case: `below_mount_minimum_move` -- the remaining correction needs a component
    smaller than the smallest angular move the mount accepts (`success` then says whether the
    residual is within `rough_tolerance_px`, the criterion `max_pulses` uses, or within the
    mount's smallest move -- sqrt(2) x a floor-sized move's image shift; message says so);
    `move_cap_below_mount_minimum` -- the per-move cap (`max_pulse_ms` in the configured unit)
    is itself below that smallest move (a configuration problem); `calibration_degenerate` --
    the matrix cannot be inverted.

    Guide pulses only (S6.0d): `warnings` -- OnStepAdapter's warnings seen on any pulse;
    `tracking_off` -- a sent pulse failed and the mount was then observed NOT tracking."""

    success: bool
    pulses_issued: int
    final_offset_px: float
    reason: str
    message: str = ""
    warnings: tuple[str, ...] = ()
    tracking_off: bool = False


class CollimationRecenterPolicy:
    """Iteratively pulses the mount to drive a measured offset toward (0, 0)."""

    def __init__(
        self,
        mount: MountPort,
        calibration: CalibrationMatrix,
        config: RecenterConfig | None = None,
        *,
        clock: Clock | None = None,
        center_rate_x: float | None = None,
        max_step_px: float | None = None,
        guide_pulses: bool = False,
    ) -> None:
        """`center_rate_x`: the unit the matrix's equivalent ms are in on a mount without timed
        pulses -- pass the loaded `MountAlignmentSettings.calibration_center_rate_x` (the
        matrix was stored with it); None = that setting's own default. Unused on timed mounts.

        `max_step_px` (mounts without timed pulses): the largest image shift one angular move
        may produce, per axis, converted to arcsec through the measured matrix -- the caller
        sizes it from the camera frame (see `FocusedStarAcquisition`). None = the fixed
        `max_pulse_ms` cap in the configured unit (~60" at 8x).

        `guide_pulses` (S6.0d): correct with tracking-preserving guide pulses; `calibration` is
        then a guide-pulse calibration in real ms, and `max_step_px` caps each guide pulse the
        same way (None = the adapter's longest pulse)."""
        self._mount = mount
        self._calibration = calibration
        self._config = config or RecenterConfig()
        self._clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        self._center_rate_x = (
            center_rate_x
            if center_rate_x is not None
            else MountAlignmentSettings().calibration_center_rate_x
        )
        self._max_step_px = max_step_px
        self._guide_pulses = guide_pulses

    def center(
        self,
        measure: Callable[[], TrackingResult],
        reference: tuple[float, float],
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> MountCorrectionResult:
        cfg = self._config
        ref_x, ref_y = reference
        pulses = 0
        prev_dist: float | None = None
        diverge_count = 0
        timed = bool(self._mount.capabilities().supports_pulse_guiding)
        warnings: set[str] = set()

        def finish(result: MountCorrectionResult) -> MountCorrectionResult:
            if not warnings:
                return result
            return MountCorrectionResult(
                result.success, result.pulses_issued, result.final_offset_px, result.reason,
                result.message, tuple(sorted(warnings)), result.tracking_off,
            )

        for _ in range(cfg.max_iterations):
            if cancel_check is not None and cancel_check():
                return finish(MountCorrectionResult(False, pulses, prev_dist or 0.0, "cancelled"))

            result = measure()
            if result.state not in _LOCKED_STATES or result.x is None or result.y is None:
                return finish(MountCorrectionResult(False, pulses, 999.0, "star_lost"))

            dx = result.x - ref_x
            dy = result.y - ref_y
            dist = (dx**2 + dy**2) ** 0.5
            if dist <= cfg.fine_tolerance_px:
                return finish(MountCorrectionResult(True, pulses, dist, "within_tolerance"))

            if prev_dist is not None and dist > prev_dist * 1.1:
                diverge_count += 1
                if diverge_count >= cfg.max_diverge_count:
                    return finish(MountCorrectionResult(False, pulses, dist, "diverging"))
            else:
                diverge_count = max(0, diverge_count - 1)
            prev_dist = dist

            planned = self._plan(dx, dy, timed)
            if isinstance(planned, _NotSent):
                if planned.reason == "below_mount_minimum_move":
                    return finish(self._below_floor_result(pulses, dist, planned.message))
                return finish(
                    MountCorrectionResult(False, pulses, dist, planned.reason, planned.message)
                )
            sent, stopped = self._send_steps(planned, warnings, cancel_check)
            pulses += sent
            if stopped is not None:
                reason, message, tracking_off = stopped
                return finish(
                    MountCorrectionResult(
                        False, pulses, dist, reason, message, tracking_off=tracking_off
                    )
                )

        final = measure()
        if final.state in _LOCKED_STATES and final.x is not None and final.y is not None:
            final_dist = ((final.x - ref_x) ** 2 + (final.y - ref_y) ** 2) ** 0.5
        else:
            final_dist = 999.0
        success = final_dist <= cfg.rough_tolerance_px
        return finish(MountCorrectionResult(success, pulses, final_dist, "max_pulses"))

    def _send_steps(
        self,
        planned: list[_CorrectionStep],
        warnings: set[str],
        cancel_check: Callable[[], bool] | None,
    ) -> tuple[int, tuple[str, str, bool] | None]:
        """Send one correction's steps (settling after each); returns how many were sent and,
        when the correction ended early, (reason, message, tracking_off)."""
        sent = 0
        for send in planned:
            # S6.0d review (A2): with guide pulses, re-check between the pulses of one
            # correction too (e.g. the operating mode left Astronomical).
            if self._guide_pulses and cancel_check is not None and cancel_check():
                return sent, ("cancelled", "", False)
            pulse_result = send()
            sent += 1
            tracking_off = _note_guide_outcome(pulse_result, warnings)
            if not pulse_result.accepted:
                _log.warning("recentering correction refused: %s", pulse_result.message)
                return sent, ("pulse_rejected", pulse_result.message, tracking_off)
            if self._config.settle_ms > 0:
                self._clock.sleep(self._config.settle_ms / 1000.0)
        return sent, None

    def _plan(self, dx: float, dy: float, timed: bool) -> list[_CorrectionStep] | _NotSent:
        if self._guide_pulses:
            return self._guide_correction(dx, dy)
        return self._timed_correction(dx, dy) if timed else self._angular_correction(dx, dy)

    def _fastest_px_per_ms(self, axis: MountAxis) -> float:
        return max(
            max(self._calibration.response_for(axis, d).px_per_ms for d in AxisDirection), 1e-9
        )

    def _guide_correction(self, dx: float, dy: float) -> list[_CorrectionStep] | _NotSent:
        """S6.0d: at most one guide pulse per axis, solved in 2-D (see the module docstring)."""
        guide_pulse = getattr(self._mount, "guide_pulse", None)
        bounds = getattr(self._mount, "guide_pulse_range_ms", None)
        if guide_pulse is None or bounds is None:
            return _NotSent("pulse_rejected", _NO_GUIDE_PULSES)
        shortest, longest = bounds
        try:
            solved = solve_screen_move(self._calibration, target_dx_px=-dx, target_dy_px=-dy)
        except ValueError as exc:
            return _NotSent("calibration_degenerate", str(exc))
        steps: list[_CorrectionStep] = []
        for axis, direction, duration_ms in solved:
            cap = longest
            if self._max_step_px is not None:
                cap_ms = int(self._max_step_px / self._fastest_px_per_ms(axis))
                cap = min(longest, max(shortest, cap_ms))
            duration = min(int(duration_ms), cap)
            if duration < shortest:
                continue  # sub-pixel at any practical guide rate
            steps.append(partial(guide_pulse, axis, direction, duration))
        if not steps:
            return _NotSent(
                "below_mount_minimum_move",
                f"the remaining correction is shorter than the shortest guide pulse "
                f"({shortest} ms) -- nothing was sent",
            )
        return steps

    def _timed_correction(self, dx: float, dy: float) -> list[_CorrectionStep]:
        """One timed pulse on the dominant axis (unchanged pre-S6.0b behaviour)."""
        axis = MountAxis.AXIS1 if abs(dx) >= abs(dy) else MountAxis.AXIS2
        offset_component = dx if axis is MountAxis.AXIS1 else dy
        direction = self._direction_opposing(axis, offset_component)
        axis_response = self._calibration.response_for(axis, direction)
        px_per_ms = max(axis_response.px_per_ms, 1e-9)
        duration_ms = max(
            1, min(self._config.max_pulse_ms, round(abs(offset_component) / px_per_ms))
        )
        return [partial(self._mount.pulse_axis, axis, direction, duration_ms)]

    def _angular_correction(self, dx: float, dy: float) -> list[_CorrectionStep] | _NotSent:
        """The angular moves (at most one per axis) that bring the star from (dx, dy) toward
        the reference, decided as a whole before anything is sent -- or why nothing can be."""
        move_angular = getattr(self._mount, "move_angular", None)
        if move_angular is None:
            return _NotSent("pulse_rejected", _NO_MOTION_PRIMITIVE)
        floor = float(getattr(self._mount, "min_angular_arcsec", 0.0) or 0.0)
        rate = self._center_rate_x
        axis_caps = self._axis_caps_arcsec(floor)
        cap = equivalent_ms_to_arcsec(self._config.max_pulse_ms, center_rate_x=rate)
        if axis_caps is None and cap < floor:
            return _NotSent(
                "move_cap_below_mount_minimum",
                f'the per-move cap ({cap:.1f}" = max_pulse_ms {self._config.max_pulse_ms} at '
                f"{rate:g}x sidereal) is below the smallest move the mount accepts "
                f'({floor:.0f}") -- check calibration_center_rate_x; nothing was sent',
            )
        # The FULL correction is solved and each component capped on its own (re-review of
        # S6.0b): scaling the whole vector to the cap pushed the smaller-scale axis (RA away
        # from the equator moves the image cos(dec) times as far) below the floor and refused
        # everything. A per-component cap changes the step's direction, which this loop
        # re-measures anyway; with the (near-)orthogonal RA/Dec image axes every capped
        # component still shrinks its own share of the error. Drops are judged against the
        # full error -- what is actually left to correct.
        try:
            solved = solve_screen_move(self._calibration, target_dx_px=-dx, target_dy_px=-dy)
        except ValueError as exc:
            return _NotSent("calibration_degenerate", str(exc))
        plan = plan_angular_move(
            self._calibration,
            solved,
            target_px=math.hypot(dx, dy),
            center_rate_x=rate,
            cap_arcsec=cap,
            floor_arcsec=floor,
            drop_tolerance=RECENTER_DROP_TOLERANCE,
            axis_cap_arcsec=axis_caps,
        )
        if plan.refused or not plan.steps:
            needs = ", ".join(f'{axis.name} {arcsec:.1f}"' for axis, arcsec in plan.dropped)
            return _NotSent(
                "below_mount_minimum_move",
                f"the remaining correction needs {needs or 'less than 1 ms equivalent'}, "
                f'below the smallest move the mount accepts ({floor:.0f}") -- nothing was sent',
            )
        return [
            partial(move_angular, step.axis, step.direction, step.arcsec) for step in plan.steps
        ]

    def _below_floor_result(self, pulses: int, dist: float, message: str) -> MountCorrectionResult:
        """`below_mount_minimum_move`: nothing more can be sent. A success when the residual is
        within the rough tolerance -- or within the mount's smallest move (sqrt(2) x the image
        shift of a floor-sized move on the faster axis: one sub-floor remainder per axis), which
        at fine plate scales (30" > 20 px at ~1"/px) is as close as this mount can get."""
        if self._guide_pulses:
            bounds = getattr(self._mount, "guide_pulse_range_ms", None)
            floor_ms = float(bounds[0]) if bounds else 0.0
        else:
            floor = float(getattr(self._mount, "min_angular_arcsec", 0.0) or 0.0)
            floor_ms = arcsec_to_equivalent_ms(floor, center_rate_x=self._center_rate_x)
        floor_px = floor_ms * max(r.px_per_ms for r in self._calibration.responses.values())
        reachable_px = math.sqrt(2.0) * floor_px
        if dist <= self._config.rough_tolerance_px:
            return MountCorrectionResult(True, pulses, dist, "below_mount_minimum_move", message)
        if dist <= reachable_px:
            return MountCorrectionResult(
                True,
                pulses,
                dist,
                "below_mount_minimum_move",
                f"within the mount's smallest move of the target (residual {dist:.1f} px <= "
                f"{reachable_px:.1f} px); {message}",
            )
        return MountCorrectionResult(False, pulses, dist, "below_mount_minimum_move", message)

    def _axis_caps_arcsec(self, floor: float) -> dict[MountAxis, float] | None:
        """Per-axis cap from `max_step_px`: the arcsec that shifts the image `max_step_px` in
        that axis' faster measured direction (never below the mount's smallest move); None
        without `max_step_px`."""
        if self._max_step_px is None:
            return None
        caps: dict[MountAxis, float] = {}
        for axis in MountAxis:
            px_per_ms = max(
                self._calibration.response_for(axis, direction).px_per_ms
                for direction in AxisDirection
            )
            ms = self._max_step_px / max(px_per_ms, 1e-9)
            caps[axis] = max(floor, equivalent_ms_to_arcsec(ms, center_rate_x=self._center_rate_x))
        return caps

    def _direction_opposing(self, axis: MountAxis, offset_component: float) -> AxisDirection:
        """Pick the AxisDirection whose calibrated response opposes offset_component."""
        positive_response = self._calibration.response_for(axis, AxisDirection.POSITIVE)
        positive_component = (
            positive_response.dx_px if axis is MountAxis.AXIS1 else positive_response.dy_px
        )
        if (positive_component > 0) != (offset_component > 0):
            return AxisDirection.POSITIVE
        return AxisDirection.NEGATIVE
