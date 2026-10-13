"""Mount Align calibration move sizing and net-displacement accounting (S6.6a, moved out of the
panel unchanged; issue #46, S6.0).

One `CalibrationSizing` per panel holds the CURRENT run's sizing state: whether moves are sized
from the cameras' optics, the per-axis duration/angle, the rate preset, the probe attempts, the
per-axis net commanded displacement (so an abort can return the mount) and the sizing log that
diagnostics report. The mount and the settings are read through injected callables, so a
caller that swaps either keeps working exactly as before. No Qt.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, AxisResponse, MountAxis, MountPort
from astrotool_core.mount.movement_sizing import (
    CameraGeometry,
    SizingPolicy,
    SizingStatus,
    StepAction,
    decide_next,
    measured_fraction,
    measured_rate_arcsec_per_s,
    plan_first_move,
    plan_followup,
)

from collimation_tool.application.equivalent_duration import angular_unit_rate_arcsec_per_s
from collimation_tool.application.mount_align_failures import (
    NO_MOTION_MIN_PX,
    NO_MOTION_WIDTH_FRACTION,
    MeasurementFailureClass,
    bounded_failure_class,
)
from collimation_tool.application.mount_align_motion import MotionStep
from collimation_tool.application.mount_align_plan import CalibrationStep, CalibrationStepRole

ExcludeNoMotion = Callable[[str, int], None]
ExcludeBounded = Callable[[str, MeasurementFailureClass, str], None]


def mount_supports_angular(mount: MountPort) -> bool:
    """The mount can install a measured rate and move by angle (OnStepAdapter >= 0.3.5) -- as
    REPORTED by the adapter (`MountCapabilities.supports_angular_moves`, S6.6b; was a duck-typed
    `hasattr` probe, #55 audit C02)."""
    return mount.capabilities().supports_angular_moves


def mount_is_angular_only(mount: MountPort) -> bool:
    """S6.0: angular moves exist but timed pulses do not -- both read from the adapter's one
    reported `MountCapabilities` (S6.6b)."""
    capabilities = mount.capabilities()
    return capabilities.supports_angular_moves and not capabilities.supports_pulse_guiding


def angular_unit_rate(settings: MountAlignmentSettings) -> float:
    """Angular-only mounts: arcsec per second of 'equivalent duration'. The sizing machinery
    (and every `AxisResponse.duration_ms`) still speaks durations; on a mount that only moves by
    angle they are converted at this ONE fixed factor -- the same centering-rate seed
    `plan_first_move` sizes with -- so a sized duration maps back to exactly the calculated
    arcsec. A unit conversion, not a mount-rate model; its single owner is
    `collimation_tool.application.equivalent_duration` (S6.0b), shared with the guide-assisted
    recenter policy that reads the matrix the panel stores."""
    return angular_unit_rate_arcsec_per_s(settings.calibration_center_rate_x)


def min_angular_arcsec(mount: MountPort) -> float:
    """The smallest angular move the mount accepts, as REPORTED by the adapter
    (`MountCapabilities.min_angular_arcsec` <- OnStepAdapter's own bound, S6.6b; was a
    duck-typed `getattr` of an adapter property); 0 for a mount that reports none."""
    return mount.capabilities().min_angular_arcsec


class CalibrationSizing:
    """Issue #46 sizing state for the CURRENT calibration run (see `start`)."""

    def __init__(
        self,
        *,
        mount: Callable[[], Any],
        settings: Callable[[], MountAlignmentSettings],
    ) -> None:
        self._mount = mount
        self._settings = settings
        self.policy: SizingPolicy | None = None
        self.geometry: dict[str, CameraGeometry] = {}
        self.axis_duration_ms: dict[MountAxis, int] = {}
        self.axis_attempt: dict[MountAxis, int] = {MountAxis.AXIS1: 1, MountAxis.AXIS2: 1}
        self.rate_preset: str | None = ""
        #: Issue #31/#46 + AGENTS.md: with an angular-capable mount (OnStepAdapter >= 0.3.5) the
        #: first move per direction is a TIMED bootstrap at the controller's centering rate; the
        #: measured rate is installed into the adapter and every later move of that direction
        #: is ANGULAR (`axis_arcsec` = the calculated ~25%-frame size, rescaled from measured
        #: pixels). Off when the mount lacks the capability or the optics are unknown.
        self.angular_active = False
        #: S6.0 (#46/#31, audit P01): the mount moves ONLY angularly (it reports no timed
        #: pulses -- OnStepAdapter >= 0.4 over INDI). Every calibration step is then an angular
        #: move sized up front from the optics; no timed bootstrap, no rate learning needed.
        self.angular_only = False
        self.axis_arcsec: dict[MountAxis, float] = {}
        #: Net sky displacement (arcsec) per axis this run -- the angular counterpart of
        #: `axis_net_pulse_ms`, which an abort's return is sized from on an angular run.
        self.axis_net_arcsec: dict[MountAxis, float] = {MountAxis.AXIS1: 0.0, MountAxis.AXIS2: 0.0}
        self.distance_m: float | None = None
        #: Per camera: why sizing could not bring its move into band (shown at the end).
        self.bounded: dict[str, str] = {}
        #: Issue #46 evidence: every sizing decision this run (diagnostics `calibration_sizing`).
        self.log: list[dict[str, Any]] = []
        #: Each axis's net commanded displacement so far this run (ms) -- see the panel's
        #: `_abort_calibration`, which returns the mount by exactly this net.
        self.axis_net_pulse_ms: dict[MountAxis, int] = {MountAxis.AXIS1: 0, MountAxis.AXIS2: 0}

    # ---- run setup ----------------------------------------------------------------------
    def reset_net(self) -> None:
        self.axis_net_pulse_ms = {MountAxis.AXIS1: 0, MountAxis.AXIS2: 0}
        self.axis_net_arcsec = {MountAxis.AXIS1: 0.0, MountAxis.AXIS2: 0.0}

    def start(
        self,
        *,
        angular_only: bool,
        cameras: list[CameraGeometry] | None,
        distance_m: float | None,
    ) -> None:
        """Decide (per run) whether moves are sized from the cameras' optics.

        Sizing is active only with a geometry provider (`cameras` not None) AND at least one
        camera with a known plate scale; otherwise the legacy fixed `pulse_ms` / `rate_preset`
        calibration runs unchanged. The seed is only a seed -- `probe` closes the loop on
        MEASURED pixels."""
        settings = self._settings()
        self.policy = None
        self.geometry = {}
        self.bounded = {}
        self.log = []
        self.rate_preset = settings.rate_preset
        self.angular_active = False
        self.angular_only = angular_only
        if self.angular_only:
            # S6.0: no timed primitive exists, so every step -- even without known optics
            # (then the fixed `pulse_ms` size, expressed angularly) -- is an angular move.
            self.angular_active = True
            self.rate_preset = None
        self.axis_arcsec = {}
        self.distance_m = None
        self.axis_duration_ms = {
            MountAxis.AXIS1: settings.pulse_ms,
            MountAxis.AXIS2: settings.pulse_ms,
        }
        self.axis_attempt = {MountAxis.AXIS1: 1, MountAxis.AXIS2: 1}
        if cameras is None:
            return
        angular = mount_supports_angular(self._mount())
        policy = SizingPolicy(
            rate_preset=settings.calibration_rate_preset,
            center_rate_x=settings.calibration_center_rate_x if angular else None,
            target_fraction=settings.calibration_target_fraction,
            max_duration_ms=settings.max_calibration_pulse_ms,
            min_duration_ms=settings.min_calibration_pulse_ms,
        )
        if self.angular_only:
            policy = self.angular_only_policy(policy, cameras, distance_m)
        plan = plan_first_move(
            cameras, policy, distance_m=distance_m, fallback_ms=settings.pulse_ms
        )
        if plan.status is SizingStatus.NO_OPTICS:
            return
        self.policy = policy
        self.geometry = {camera.key: camera for camera in cameras}
        self.distance_m = distance_m
        self.angular_active = angular
        # Angular mode: no rate preset anywhere -- OnStepAdapter's centering rate is the one
        # rate the timed bootstrap, the installed calibration and the angular moves share.
        self.rate_preset = None if angular else policy.rate_preset
        if angular and plan.target_arcsec is not None:
            self.axis_arcsec = {
                MountAxis.AXIS1: plan.target_arcsec,
                MountAxis.AXIS2: plan.target_arcsec,
            }
        self.axis_duration_ms = {
            MountAxis.AXIS1: plan.duration_ms,
            MountAxis.AXIS2: plan.duration_ms,
        }
        self.log.append(
            {
                "event": "seed",
                "duration_ms": plan.duration_ms,
                "seed_camera": plan.seed_camera,
                "status": plan.status.value,
                "rate_preset": self.rate_preset,
                "distance_m": distance_m,
                "angular": angular,
                "target_arcsec": plan.target_arcsec,
            }
        )

    def unit_rate(self) -> float:
        return angular_unit_rate(self._settings())

    def min_angular_ms(self) -> int:
        """`min_angular_arcsec` in the angular-only duration unit, rounded UP so the converted
        move never falls below the floor."""
        return math.ceil(min_angular_arcsec(self._mount()) / self.unit_rate() * 1000.0)

    def angular_only_policy(
        self, policy: SizingPolicy, cameras: list[CameraGeometry], distance_m: float | None
    ) -> SizingPolicy:
        """Angular-only mounts: the duration envelope (`max_calibration_pulse_ms`) guarded a
        TIMED move; an angular move's size is explicit. Widen it, from the configured optics
        only, so the widest participating camera can still get the one larger follow-up the
        #46 smallest-FOV-first strategy gives it (up to the band's upper edge of ITS frame).

        The lower bound is raised to the mount's own smallest accepted angular move
        (`min_angular_arcsec`, reported by the adapter): a resized probe can never be
        commanded below it (review of S6.0: a 25%-of-110" Dec probe = 27.4" was refused)."""
        policy = replace(policy, min_duration_ms=max(policy.min_duration_ms, self.min_angular_ms()))
        widths = [fov[0] for camera in cameras if (fov := camera.fov_arcsec(distance_m))]
        if not widths:
            return policy
        unit = self.unit_rate()
        needed_ms = math.ceil(policy.band_high * max(widths) / unit * 1000.0)
        return replace(policy, max_duration_ms=max(policy.max_duration_ms, needed_ms))

    # ---- sizes --------------------------------------------------------------------------
    def return_chunks(self, total_ms: int, cap_ms: int) -> list[int]:
        """Split an unmeasured return move into back-to-back chunks of at most `cap_ms`.

        Angular-only mounts: chunks are sized EVENLY so no remainder falls below the mount's
        smallest move; a total below that floor cannot be commanded at all and yields []
        (the caller reports the unreturned residual). Other mounts: unchanged (full chunks
        plus the remainder)."""
        cap = max(1, cap_ms)
        if total_ms <= 0:
            return []
        if self.angular_only:
            floor_ms = self.min_angular_ms()
            if total_ms < floor_ms:
                return []
            count = math.ceil(total_ms / max(cap, floor_ms))
            base, extra = divmod(total_ms, count)
            return [base + (1 if index < extra else 0) for index in range(count)]
        chunks: list[int] = []
        remaining = total_ms
        while remaining > 0:
            chunks.append(min(remaining, cap))
            remaining -= chunks[-1]
        return chunks

    def format_size(self, duration_ms: int) -> str:
        """A move size for a message: arcsec on an angular-only mount, else ms."""
        if self.angular_only:
            return f'{duration_ms * self.unit_rate() / 1000.0:.0f}"'
        return f"{duration_ms} ms"

    def rate_for(self, axis: MountAxis, direction: AxisDirection) -> float | None:
        """Arcsec per second used to express this direction's moves as durations: the fixed
        unit rate on an angular-only mount, else the MEASURED rate installed by a timed
        bootstrap (None until then, or when angular moves are off)."""
        if not self.angular_active:
            return None
        if self.angular_only:
            return self.unit_rate()
        rate = self._mount().installed_rate(axis, direction)
        return float(rate) if rate else None

    def installed_rate(self, step: CalibrationStep) -> float | None:
        return self.rate_for(step.axis, step.direction)

    def motion_step(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int
    ) -> MotionStep:
        """An unmeasured corrective step: angular on an angular-only mount, else timed."""
        if self.angular_only:
            arcsec = self.unit_rate() * duration_ms / 1000.0
            return (axis, direction, duration_ms, arcsec)
        return (axis, direction, duration_ms)

    def step_duration_ms(self, step: CalibrationStep) -> int:
        """The step's size as a duration. With an installed rate an axis-sized step is the
        calculated angular size expressed in that rate's time (the equivalent duration the
        response is normalised by and the timed fallback would run), bounded like any move."""
        if step.duration_ms is not None:
            return step.duration_ms
        rate = self.installed_rate(step)
        arcsec = self.axis_arcsec.get(step.axis)
        policy = self.policy
        if rate is not None and arcsec is not None and policy is not None:
            ms = arcsec / rate * 1000.0
            return int(round(max(policy.min_duration_ms, min(policy.max_duration_ms, ms))))
        return self.axis_duration_ms[step.axis]

    def step_arcsec(self, step: CalibrationStep, duration_ms: int) -> float | None:
        """Angular size of this step, or None = run it as a timed (bootstrap) move: no
        angular capability/optics, or no rate installed yet for this direction. Never None on
        an angular-only mount (S6.0): its first move is sized up front, never bootstrapped."""
        rate = self.installed_rate(step)
        if rate is None:
            return None
        return rate * duration_ms / 1000.0

    def is_probe(self, step: CalibrationStep) -> bool:
        """The axis's first POSITIVE/FIRST move sizes the whole axis."""
        return (
            self.policy is not None
            and step.only_camera is None
            and step.duration_ms is None
            and step.direction is AxisDirection.POSITIVE
            and step.role is CalibrationStepRole.FIRST
        )

    def smallest_fov_key(self, keys: list[str]) -> str | None:
        sized = []
        for key in keys:
            geometry = self.geometry.get(key)
            fov = geometry.fov_arcsec() if geometry is not None else None
            if fov is not None:
                sized.append((fov[0], key))
        return min(sized)[1] if sized else None

    # ---- accounting ---------------------------------------------------------------------
    def account_pulse(self, step: CalibrationStep, duration_ms: int) -> None:
        """Issue #31: every completed calibration pulse adds to its axis's net commanded
        displacement, so an aborted run can be returned (a finished axis group nets to 0)."""
        self.axis_net_pulse_ms[step.axis] += (
            duration_ms if step.direction is AxisDirection.POSITIVE else -duration_ms
        )

    def account_arcsec(
        self, step: CalibrationStep, duration_ms: int, angular_arcsec: float | None
    ) -> None:
        """Add this move's sky displacement to the axis net: the commanded angular size, or
        the installed rate x duration for a timed move (0 while the rate is still unknown --
        `learn_rate` adds the bootstrap move's own measured size)."""
        if not self.angular_active:
            return
        arcsec = angular_arcsec
        if arcsec is None:
            rate = self.installed_rate(step)
            arcsec = rate * duration_ms / 1000.0 if rate else 0.0
        sign = 1.0 if step.direction is AxisDirection.POSITIVE else -1.0
        self.axis_net_arcsec[step.axis] += sign * arcsec

    def net_return(self, axis: MountAxis) -> tuple[AxisDirection, int]:
        """Direction and duration that undo this axis's net displacement. Angular runs size it
        from the net sky arcsec through the return direction's own rate."""
        net_arcsec = self.axis_net_arcsec.get(axis, 0.0)
        if self.angular_active and net_arcsec != 0.0:
            direction = AxisDirection.NEGATIVE if net_arcsec > 0 else AxisDirection.POSITIVE
            rate = self.rate_for(axis, direction)
            if rate:
                return direction, int(round(abs(net_arcsec) / rate * 1000.0))
        net_ms = self.axis_net_pulse_ms[axis]
        return (AxisDirection.NEGATIVE if net_ms > 0 else AxisDirection.POSITIVE), abs(net_ms)

    def log_motion(
        self,
        step: CalibrationStep,
        *,
        angular_arcsec: float | None,
        duration_ms: int,
        motion_paths: tuple[str, ...],
    ) -> None:
        if not self.angular_active:
            return
        self.log.append(
            {
                "event": "motion",
                "axis": step.axis.name,
                "direction": step.direction.name,
                "path": motion_paths[-1] if motion_paths else None,
                "angular_arcsec": angular_arcsec,
                "duration_ms": duration_ms,
            }
        )

    def learn_rate(
        self,
        step: CalibrationStep,
        *,
        angular_arcsec: float | None,
        duration_ms: int,
        responses: dict[str, AxisResponse],
    ) -> None:
        """A TIMED bootstrap move yields this direction's real centering rate (measured image
        shift x plate scale / duration); it is installed into the mount so every later move
        of the direction is angular. A move that ran angular (or fell back) never re-derives
        it, and an unmeasurable camera or a shift below the no-motion floor teaches nothing."""
        if (
            not self.angular_active
            or angular_arcsec is not None
            or step.role is CalibrationStepRole.RETURN
            or not responses
        ):
            return
        key = self.smallest_fov_key(list(responses)) or next(iter(responses))
        geometry = self.geometry.get(key)
        response = responses[key]
        if geometry is None or response.magnitude_px < NO_MOTION_MIN_PX:
            return
        rate = measured_rate_arcsec_per_s(
            geometry,
            response.dx_px,
            response.dy_px,
            duration_ms,
            distance_m=self.distance_m,
        )
        if rate is None or rate <= 0:
            return
        first_for_direction = self.installed_rate(step) is None
        self._mount().install_rate(step.axis, step.direction, rate)
        if first_for_direction:  # the bootstrap move itself was not accounted yet
            sign = 1.0 if step.direction is AxisDirection.POSITIVE else -1.0
            self.axis_net_arcsec[step.axis] += sign * rate * duration_ms / 1000.0
        self.log.append(
            {
                "event": "rate_installed",
                "axis": step.axis.name,
                "direction": step.direction.name,
                "camera": key,
                "arcsec_per_s": round(rate, 4),
                "from_duration_ms": duration_ms,
            }
        )

    # ---- adaptive resizing (issue #46) ----------------------------------------------------
    def probe(
        self,
        step: CalibrationStep,
        duration_ms: int,
        responses: dict[str, AxisResponse],
        *,
        exclude_no_motion: ExcludeNoMotion,
        exclude_bounded: ExcludeBounded,
    ) -> bool:
        """Adaptive resizing on the MEASURED displacement of the smallest-FOV camera. True =
        the probe was too small/large: it is discarded (no response recorded) and the axis
        duration rescaled -- the caller re-queues the same step (bounded by
        `SizingPolicy.max_attempts`). A camera the policy cannot bring into band is excluded
        through the callbacks and dropped from `responses`."""
        policy = self.policy
        if policy is None or not self.is_probe(step):
            return False
        seed_key = self.smallest_fov_key(list(responses))
        if seed_key is None:
            return False
        response = responses[seed_key]
        geometry = self.geometry[seed_key]
        fraction = measured_fraction(geometry, response.dx_px, response.dy_px)
        attempt = self.axis_attempt[step.axis]
        decision = decide_next(duration_ms, fraction, policy, attempt=attempt)
        self.log.append(
            {
                "event": "probe",
                "axis": step.axis.name,
                "camera": seed_key,
                "duration_ms": duration_ms,
                "measured_fraction": round(fraction, 4),
                "attempt": attempt,
                "action": decision.action.value,
                "next_duration_ms": decision.duration_ms,
                "reason": decision.reason,
            }
        )
        if decision.action is StepAction.RETRY:
            if step.axis in self.axis_arcsec and duration_ms > 0:
                self.axis_arcsec[step.axis] *= decision.duration_ms / duration_ms
            self.axis_duration_ms[step.axis] = decision.duration_ms
            self.axis_attempt[step.axis] = attempt + 1
            return True
        if decision.action is StepAction.BOUNDED:
            failure = bounded_failure_class(decision.reason)
            if fraction < NO_MOTION_WIDTH_FRACTION:
                exclude_no_motion(seed_key, duration_ms)
            else:
                exclude_bounded(seed_key, failure, decision.reason or "bounded")
            responses.pop(seed_key, None)
        if step.axis is MountAxis.AXIS1:
            # RA and Dec share the mount's rate: start Dec from RA's accepted size.
            self.axis_duration_ms[MountAxis.AXIS2] = self.axis_duration_ms[MountAxis.AXIS1]
            if MountAxis.AXIS1 in self.axis_arcsec:
                self.axis_arcsec[MountAxis.AXIS2] = self.axis_arcsec[MountAxis.AXIS1]
        return False

    def axis_group_followups(
        self,
        step: CalibrationStep,
        *,
        failed_cameras: set[str],
        partial: dict[str, dict[MountAxis, AxisResponse]],
        exclude_no_motion: ExcludeNoMotion,
        exclude_bounded: ExcludeBounded,
    ) -> list[CalibrationStep]:
        """After an axis's last step (issue #46): the steps to run next -- capped, unmeasured
        RETURN pulses that undo the net displacement left by discarded probes, and a larger
        follow-up for any camera that saw too little of its frame, measured for that camera
        only, so narrow-camera results survive. [] when this step does not end a sized group."""
        policy = self.policy
        if (
            policy is None
            or step.only_camera is not None
            or step.direction is not AxisDirection.NEGATIVE
            or step.role is not CalibrationStepRole.REPEAT
        ):
            return []
        axis = step.axis
        inserts: list[CalibrationStep] = []
        direction, remaining = self.net_return(axis)
        chunks = self.return_chunks(remaining, policy.max_duration_ms)
        if remaining > 0 and not chunks:
            self.log.append(
                {"event": "return_skipped", "axis": axis.name, "duration_ms": remaining}
            )
        for chunk in chunks:
            inserts.append(
                CalibrationStep(axis, direction, CalibrationStepRole.RETURN, duration_ms=chunk)
            )
        accepted_ms = self.axis_duration_ms[axis]
        for key in ("left", "right"):
            if key in failed_cameras:
                continue
            response = partial[key].get(axis)
            geometry = self.geometry.get(key)
            if response is None or geometry is None:
                continue
            fraction = measured_fraction(geometry, response.dx_px, response.dy_px)
            decision = plan_followup(accepted_ms, fraction, policy)
            if decision.action is StepAction.ACCEPT:
                continue
            self.log.append(
                {
                    "event": "followup",
                    "axis": axis.name,
                    "camera": key,
                    "measured_fraction": round(fraction, 4),
                    "action": decision.action.value,
                    "duration_ms": decision.duration_ms,
                    "reason": decision.reason,
                }
            )
            if decision.action is StepAction.BOUNDED:
                if fraction < NO_MOTION_WIDTH_FRACTION:
                    exclude_no_motion(key, accepted_ms)
                else:
                    exclude_bounded(
                        key,
                        MeasurementFailureClass.MOVE_EXCEEDS_ENVELOPE,
                        decision.reason or "bounded",
                    )
                continue
            for direction_, role in (
                (AxisDirection.POSITIVE, CalibrationStepRole.FIRST),
                (AxisDirection.POSITIVE, CalibrationStepRole.REPEAT),
                (AxisDirection.NEGATIVE, CalibrationStepRole.FIRST),
                (AxisDirection.NEGATIVE, CalibrationStepRole.REPEAT),
            ):
                inserts.append(
                    CalibrationStep(
                        axis,
                        direction_,
                        role,
                        duration_ms=decision.duration_ms,
                        only_camera=key,
                    )
                )
        return inserts

    def abort_return(self, axis: MountAxis, cap_ms: int) -> tuple[list[MotionStep], str]:
        """The unmeasured moves that return `axis` to its start after an aborted run, and the
        note for the failure message. Issue #43/#46: never one pulse beyond the normal envelope
        (`cap_ms`) -- the net left by several growth probes is returned as capped pulses run
        back-to-back; a residual below the mount's smallest move cannot be returned."""
        return_direction, return_ms = self.net_return(axis)
        chunks = self.return_chunks(return_ms, cap_ms)
        if chunks:
            suffix = " (returning mount to start position…)"
        elif return_ms != 0:
            suffix = (
                f" (residual {self.format_size(return_ms)} is below the mount's smallest "
                "move -- not returned)"
            )
        else:
            suffix = ""
        return [self.motion_step(axis, return_direction, chunk) for chunk in chunks], suffix
