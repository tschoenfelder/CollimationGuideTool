"""Mount Align calibration results for the CURRENT run (S6.6a, moved out of the panel unchanged).

Per camera: which cameras were excluded, the issue #31 Phase C FIRST/REPEAT pairs and their
`DirectionCharacterization`s, the POSITIVE-direction steady state per axis (`partial`, the
shape `diagnostic_context()` reports), and the end-of-run verdict -- a `CalibrationMatrix` per
usable camera plus the per-camera summary lines and failure classes. No Qt, no device access.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from astrotool_core.mount import (
    AxisDirection,
    AxisResponse,
    CalibrationMatrix,
    CalibrationUnit,
    DirectionCharacterization,
    MountAxis,
    is_degenerate,
)

from collimation_tool.application.mount_align_failures import MeasurementFailureClass
from collimation_tool.application.mount_align_measurement import Measurement
from collimation_tool.application.mount_align_messages import (
    CAMERA_LABELS,
    OTHER_CAMERA,
    degenerate_calibration_message,
    format_response,
)
from collimation_tool.application.mount_align_plan import CalibrationStep, CalibrationStepRole

DirectionKey = tuple[MountAxis, AxisDirection]


@dataclass
class CalibrationVerdict:
    """The end of a calibration run: the usable matrices, one summary line per reported camera
    and the failure class of every camera that ended without one (beyond earlier ones)."""

    matrices: dict[str, CalibrationMatrix] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    failure_classes: dict[str, MeasurementFailureClass] = field(default_factory=dict)


class CalibrationResults:
    """What the current Run Calibration attempt has measured so far, per camera."""

    def __init__(self) -> None:
        self.partial: dict[str, dict[MountAxis, AxisResponse]] = {"left": {}, "right": {}}
        #: Issue #31 Phase C: this run's own per-(axis,direction) FIRST response, held until
        #: its own REPEAT arrives -- see `record_direction_response`. Combined into
        #: `characterizations` once the pair completes, never read directly otherwise.
        self.pending_first: dict[str, dict[DirectionKey, AxisResponse]] = {"left": {}, "right": {}}
        #: Issue #31 Phase C: every completed `DirectionCharacterization` this run, per camera
        #: -- the source of both the full 4-direction `CalibrationMatrix` and
        #: `diagnostic_backlash_evidence()`. `partial` stays populated the same
        #: POSITIVE-direction-only shape it always had (each direction's own `.steady_state`).
        self.characterizations: dict[str, dict[DirectionKey, DirectionCharacterization]] = {
            "left": {},
            "right": {},
        }
        #: Cameras excluded from the current attempt (real report d14c3a9b): one camera's bad
        #: signal no longer takes the other camera's calibration down with it.
        self.failed_cameras: set[str] = set()

    def reset(self) -> None:
        self.partial = {"left": {}, "right": {}}
        self.pending_first = {"left": {}, "right": {}}
        self.characterizations = {"left": {}, "right": {}}
        self.failed_cameras = set()

    def measure_step(
        self,
        step: CalibrationStep,
        before: Mapping[str, Measurement],
        after: Mapping[str, Measurement],
        build: Callable[[str, Measurement, Measurement], AxisResponse | None],
    ) -> tuple[dict[str, AxisResponse], list[str], list[str]]:
        """Per-camera measurement of one calibration step (issue #43: a confident zero is never
        accepted as evidence). Returns (responses, newly excluded cameras, the subset of those
        whose match failed -- `MeasurementFailureClass.MATCH_FAILED`)."""
        responses: dict[str, AxisResponse] = {}
        newly_failed: list[str] = []
        match_failed: list[str] = []
        for key in ("left", "right"):
            if key in self.failed_cameras:
                continue  # already excluded -- no point re-measuring it
            if step.only_camera is not None and key != step.only_camera:
                continue  # issue #46: a follow-up measures ONLY the wide camera
            if key not in before or key not in after:
                # Issue #30: this camera's own before or after capture never produced a usable
                # frame at all (its failure class was already recorded by the capture) --
                # excluded the same as any other per-camera measurement failure below, not a
                # reason to abort a camera that DID succeed.
                newly_failed.append(key)
                self.failed_cameras.add(key)
                continue
            response = build(key, before[key], after[key])
            if response is None:
                newly_failed.append(key)
                self.failed_cameras.add(key)
                match_failed.append(key)
                continue
            responses[key] = response
        return responses, newly_failed, match_failed

    def record_direction_response(
        self, camera_key: str, step: CalibrationStep, response: AxisResponse
    ) -> None:
        """Issue #31 Phase C: stashes a `FIRST` response until its own `REPEAT` arrives, then
        combines the pair into a `DirectionCharacterization` (see that type's own docstring).
        Also keeps `partial` (the POSITIVE-direction steady-state per axis) populated in exactly
        the same shape it had before that issue, so `diagnostic_context()`'s own contract
        doesn't change."""
        key = (step.axis, step.direction)
        if step.role is CalibrationStepRole.FIRST:
            self.pending_first[camera_key][key] = response
            return
        first = self.pending_first[camera_key].pop(key, None)
        if first is None:
            return  # shouldn't happen -- FIRST must have already succeeded to reach REPEAT
        characterization = DirectionCharacterization(first=first, repeat=response)
        self.characterizations[camera_key][key] = characterization
        if step.direction is AxisDirection.POSITIVE:
            self.partial[camera_key][step.axis] = characterization.steady_state

    def verdict(
        self,
        bounded: Mapping[str, str],
        unit: CalibrationUnit = CalibrationUnit.AXIS_MS,
    ) -> CalibrationVerdict:
        """Build each camera's `CalibrationMatrix`, unless its own AXIS1/AXIS2 responses are
        degenerate -- real report (diagnostic 0270868c): the driver can report a pulse fully
        accepted while producing no real, measurable mount motion. Storing that as a
        "successful" matrix used to only surface the problem later, confusingly, at the first
        nudge; checking it here means the affected camera's buttons never enable and the user
        sees a specific, actionable message immediately. `bounded`: the cameras sizing gave up
        on (issue #46), with the reason shown for each. `unit` (S6.8b): the motion primitive
        the run measured with, stored on every matrix."""
        verdict = CalibrationVerdict()
        lines = verdict.lines
        for key in ("left", "right"):
            if key in bounded:
                lines.append(f"{CAMERA_LABELS[key]}: {bounded[key]}.")
                continue
            if key in self.failed_cameras:
                # Real report d14c3a9b -- this camera's own measurement failed partway
                # through, but that no longer took the other camera's calibration down with it.
                lines.append(
                    f"{CAMERA_LABELS[key]}: not enough structure to measure a displacement "
                    "-- excluded from this calibration (driver accepted every pulse; check "
                    "this camera's own exposure/gain and target)."
                )
                continue
            axis1 = self.partial[key].get(MountAxis.AXIS1)
            axis2 = self.partial[key].get(MountAxis.AXIS2)
            if axis1 is None or axis2 is None:
                continue  # shouldn't happen unless the run was already aborted
            if is_degenerate(axis1, axis2):
                verdict.failure_classes[key] = MeasurementFailureClass.CALIBRATION_INVALID
                lines.append(
                    degenerate_calibration_message(
                        key, axis1, axis2, self.partial[OTHER_CAMERA[key]]
                    )
                )
                continue
            if any(
                characterization.steady_state.magnitude_px <= 0.0
                for characterization in self.characterizations[key].values()
            ):
                # Issue #43: EVERY direction must have produced motion, not only the two
                # POSITIVE ones is_degenerate() looks at.
                verdict.failure_classes[key] = MeasurementFailureClass.NO_MOTION_DETECTED
                lines.append(
                    f"{CAMERA_LABELS[key]}: at least one direction produced no image "
                    "displacement -- check that the mount really moves in every direction."
                )
                continue
            # Issue #31 Phase C: every measured direction's own steady-state response (not just
            # the two POSITIVE ones) -- all 4 (axis, direction) entries exist by now (the camera
            # would already be in failed_cameras, handled above, if any of its steps failed).
            verdict.matrices[key] = CalibrationMatrix(
                responses={
                    direction_key: characterization.steady_state
                    for direction_key, characterization in self.characterizations[key].items()
                },
                unit=unit,
            )
            lines.append(
                f"{CAMERA_LABELS[key]}: RA-axis {format_response(axis1)} | "
                f"Dec-axis {format_response(axis2)}"
            )
        return verdict
