"""Mount Align move/step value types (S6.6a, moved verbatim out of the panel).

The issue #31 Phase C backlash-revealing calibration sequence, the screen-move directions/sizes,
a resolved nudge and the action pending between a runner submit and its completion. No Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal

from astrotool_core.mount import AxisDirection, MountAxis

from collimation_tool.application.mount_align_measurement import Measurement, TargetMode


class ScreenDirection(Enum):
    """Issue #31 Phase D: the four on-screen directions the screen-
    relative pad understands, as a `(dx_sign, dy_sign)` unit vector in
    the same image-array convention `AxisResponse.angle_degrees` already
    documents (x right, y down) -- `solve_screen_move`'s own
    `target_dx_px`/`target_dy_px` are this vector scaled by
    `_screen_move_fraction()` times this camera's own frame width/height."""

    LEFT = (-1, 0)
    RIGHT = (1, 0)
    UP = (0, -1)
    DOWN = (0, 1)


class MovementSize(Enum):
    """Issue #31 Phase D's own frame-relative movement sizes for the
    screen-relative pad -- see `_screen_move_fraction()`. Deliberately a
    fraction of *this camera's own* frame width/height (mirrors
    `nudge_target_fraction`'s own reasoning), not a fixed pixel count."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class CalibrationStepRole(Enum):
    """Issue #31 Phase C's own backlash-revealing sequence (see
    `CALIBRATION_STEPS`): `FIRST` is a direction's own first pulse this
    run (may be backlash-contaminated -- the first commanded steps after
    a direction reversal can consume mechanical gear slack instead of
    turning the axis, see real evidence in
    `tests/local_data/test_calibration_against_real_captures.py`);
    `REPEAT` is the immediately-following second pulse in the same
    direction -- the steady-state response a `CalibrationMatrix` should
    trust (`DirectionCharacterization.steady_state`)."""

    FIRST = "first"
    REPEAT = "repeat"
    #: Issue #46: an UNMEASURED corrective pulse that undoes the net
    #: displacement left by discarded adaptive-resizing probes, so a
    #: sized run still ends with the mount where it started.
    RETURN = "return"


@dataclass(frozen=True)
class CalibrationStep:
    """One measured pulse within the backlash-revealing calibration
    sequence -- see `CALIBRATION_STEPS`'s own docstring. Unlike the old
    4-step sequence, every step is measured now -- issue #31 Phase C's
    own backlash-revealing design needs a real measurement on both the
    first and the repeated pulse in each direction to tell them apart,
    not an unmeasured, assumed-symmetric "return" pulse."""

    axis: MountAxis
    direction: AxisDirection
    role: CalibrationStepRole
    #: Issue #46: an explicit duration for this step (a wide-camera follow-up
    #: or a RETURN pulse); None = the axis's current sized duration.
    duration_ms: int | None = None
    #: Issue #46: measure ONLY this camera ('left'/'right') for this step
    #: (a wide-camera follow-up must not disturb the narrow camera's
    #: already-good result). None = every camera.
    only_camera: str | None = None


#: Issue #31 Phase C's own suggested backlash-revealing sequence, per
#: axis: + (first), + (repeat), - (first, the reversal), - (repeat) --
#: reveals whether a direction's own first pulse this run underperforms
#: relative to its own steady state (mechanical backlash/dead travel),
#: rather than assuming opposite-direction symmetry the way the old
#: 4-step sequence did. AXIS1 first is arbitrary; order between axes
#: doesn't matter functionally. No corrective "return to start" pulse is
#: appended at the end of a clean run -- each axis's own 4 pulses are two
#: full-length positive and two full-length negative (same `pulse_ms`
#: every time), so a clean run's own net residual displacement is exactly
#: zero; only a mid-sequence *abort* can leave a nonzero net, which
#: `_abort_calibration`'s own corrective pulse handles -- see that
#: method's own docstring.
CALIBRATION_STEPS: tuple[CalibrationStep, ...] = (
    CalibrationStep(MountAxis.AXIS1, AxisDirection.POSITIVE, CalibrationStepRole.FIRST),
    CalibrationStep(MountAxis.AXIS1, AxisDirection.POSITIVE, CalibrationStepRole.REPEAT),
    CalibrationStep(MountAxis.AXIS1, AxisDirection.NEGATIVE, CalibrationStepRole.FIRST),
    CalibrationStep(MountAxis.AXIS1, AxisDirection.NEGATIVE, CalibrationStepRole.REPEAT),
    CalibrationStep(MountAxis.AXIS2, AxisDirection.POSITIVE, CalibrationStepRole.FIRST),
    CalibrationStep(MountAxis.AXIS2, AxisDirection.POSITIVE, CalibrationStepRole.REPEAT),
    CalibrationStep(MountAxis.AXIS2, AxisDirection.NEGATIVE, CalibrationStepRole.FIRST),
    CalibrationStep(MountAxis.AXIS2, AxisDirection.NEGATIVE, CalibrationStepRole.REPEAT),
)


@dataclass(frozen=True)
class NudgeMove:
    """What a nudge click resolved to, carried across the (possibly asynchronous) BEFORE
    capture into the pulse submission."""

    camera_key: str
    axis: MountAxis
    direction: AxisDirection
    duration_ms: int
    clamped: bool
    #: S6.0: the angular size (arcsec) on a mount without timed pulses; None = timed.
    arcsec: float | None = None


@dataclass
class PendingAction:
    """State carried from a submit call to the matching `_poll()` completion
    -- generalizes the old single set of `_pending_*` fields to cover a
    calibration step, a direction-pad nudge, and (issue #31 Phase D) a
    screen-relative move."""

    kind: Literal["calibration", "nudge", "screen_move"]
    before: dict[str, Measurement]
    mode: TargetMode
    step: CalibrationStep | None = None
    #: Total pulse duration across every sub-pulse in a nudge's composed
    #: move (unused for a calibration step, which has its own step.axis to
    #: report against instead).
    duration_ms: int = 0
    #: Set when this calibration step runs as an ANGULAR move (arcsec); None = timed.
    angular_arcsec: float | None = None
    #: Real diagnostic 6cb859d2: True if `_on_nudge_clicked` clamped this
    #: move's duration down to `max_nudge_pulse_ms` -- see that method's
    #: own docstring. `_finish_nudge` uses this to tell the user the move
    #: landed short of the originally requested target on purpose, not a
    #: failure, and that another click continues it.
    clamped: bool = False
    #: The single axis/direction a nudge (not a calibration step, which
    #: has its own `step.axis`/`step.direction` instead) actually pulsed
    #: -- real request: direction-pad buttons are RA+/RA-/Dec+/Dec- now
    #: (see `_build_direction_pad`'s own docstring for why the earlier
    #: screen-relative Up/Down/Left/Right composed moves were replaced),
    #: so unlike the old composed move, a nudge's own axis/direction is
    #: real and known up front, not a display-only placeholder. Unused
    #: for a screen move, which can pulse either or both axes -- see
    #: `label` below instead.
    axis: MountAxis | None = None
    direction: AxisDirection | None = None
    #: Issue #31 Phase D: a screen move's own free-text description
    #: ("Main left (Small)"), shown once `_finish_screen_move` confirms
    #: the pulse sequence completed -- a screen move has no single
    #: axis/direction to describe itself from the fields above, and (see
    #: module docstring's own Phase E paragraph) never has a measured
    #: response to report instead.
    label: str | None = None


def step_label(step: CalibrationStep, phase: Literal["before", "after"]) -> str:
    """The diagnostics label of a calibration step's capture, e.g. `axis1_positive_first_after`."""
    return f"{step.axis.name.lower()}_{step.direction.name.lower()}_{step.role.value}_{phase}"
