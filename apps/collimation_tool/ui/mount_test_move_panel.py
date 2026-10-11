"""MountTestMovePanel — mount-alignment tool: calibrate how each mount axis
moves *each camera's own frame*, then offer per-camera RA+/RA-/Dec+/Dec-
buttons that each pulse exactly the one named axis and report how far that
moved this specific camera's own view.

Reworked from the original "Test Move" diagnostic (raw N/S/E/W buttons that
just reported the resulting displacement as text) into an alignment tool
requested directly: "aligning primary and secondary scope" needs a
calibrated per-camera reading of dx/dy/angle for each axis, since Main and
Guide can be rotated differently relative to each other and to the mount's
RA/Dec axes — reading those numbers after each raw-axis click and inferring
the mapping by hand doesn't scale to that; showing the calibrated response
(`format_response`) after every axis click does.

An earlier version of the direction pad instead composed a *screen-
relative* Up/Down/Left/Right move (`astrotool_core.mount.axis_calibration`'s
`compose_screen_move`, a two-axis matrix inversion solving for whichever
combination of both axes lands on-screen in the requested direction for
that camera's own rotation). Real request (diagnostic 6cb859d2's own
follow-up): a button labelled "Up" told an operator nothing about *which
physical axis* (or combination of both) was actually about to move,
confusing before ever having a feel for how a given camera sits relative to
the mount — the direction pad was replaced with one axis pulsed directly
per button instead, so the label itself (RA/Dec, this module's own
established axis-name convention -- see `AXIS_LABELS`) is never ambiguous.

Issue #31 Phase D brings screen-relative movement back, per camera, once
that camera has a full, non-degenerate 4-direction `CalibrationMatrix`: a
second pad (←/→/↑/↓, `ScreenDirection`) alongside the RA/Dec one (never
instead of it -- the RA/Dec pad is this panel's own bootstrap control,
available before any calibration exists at all, see `CALIBRATION_STEPS`'s
own Phase A note). `solve_screen_move` (`astrotool_core.mount`, wraps
`compose_screen_move` -- see that function's own docstring for why a
direction-aware wrapper was needed once opposite-direction responses
stopped being assumed symmetric) solves which axis/direction/duration
combination lands on-screen in the requested direction for that camera's
own rotation, submitted as one `MountTestMoveRunner.submit_sequence()`
call. Movement size is frame-relative (Small/Medium/Large,
`MovementSize`), not a raw duration -- see `_screen_move_fraction()`.

Its own `MountPort`, which shares the single OnStepAdapter connection with
`MountParkPanel`'s `MountParkPort` (see `astrotool_core.onstep`): motion goes
through OnStepAdapter's bounded timed-move API, never raw INDI.

Also takes the *same* `MountParkPort` object `MountParkPanel` uses
(`mount_park` constructor param — deliberately the shared instance, not
this panel's own connection: `MountTestMoveRunner` drives it directly, and
it's simplest for that to be the one connection already managed by
`MountParkPanel`'s own Connect button rather than a second, independently-
connected copy of the same park/unpark state).

Unlike the original diagnostic, this panel no longer requires the mount to
already be parked (or already unparked) before anything is clickable —
every pulse, whether a calibration step or a direction-pad nudge, goes
through `MountTestMoveRunner`, which unparks first if needed (a real-
hardware check found OnStep refuses motion commands
while parked — a deliberate safety interlock, not a defect) and then never
re-parks (`park_after=False` on every call here) — "Run Calibration" and the
direction pads are meant to run one after another across a single unparked
working session, and re-parking after each pulse would undo the point of
staying unparked between them. Parking back up when done stays the separate
Mount panel's job, same as it already is for every other unparked action in
this app.

"Run Calibration" runs a fixed eight-pulse, backlash-revealing sequence
(see `CALIBRATION_STEPS`, issue #31 Phase C): for each axis, pulse
positive (`FIRST`), pulse positive again (`REPEAT`), pulse negative
(`FIRST`, the reversal), pulse negative again (`REPEAT`) -- every pulse is
now measured (the old 4-step sequence trusted an *unmeasured*,
direction-symmetric "return" pulse for the negative direction; real
evidence (`tests/local_data/test_calibration_against_real_captures.py`)
showed a direction's own first pulse this run can measure far smaller than
its steady-state rate -- mechanical backlash/dead travel, not a
measurement bug -- so opposite directions, and a direction's own first vs.
repeated pulse, are never assumed to agree). Each direction's `FIRST`/
`REPEAT` pair becomes a `DirectionCharacterization`
(`astrotool_core.mount.axis_calibration`) -- `.steady_state` (the `REPEAT`
response) is what a camera's `CalibrationMatrix` actually stores per
`(axis, direction)`, `.backlash_deficit_px` is retained separately for
diagnostics, never used as if it were the normal rate. A camera's full
4-direction `CalibrationMatrix` becomes available once every direction
measures successfully for it (`_finish_calibration`'s own
`is_degenerate()` check on the two axes' positive-direction responses,
same guard as before), at which point that camera's RA/Dec direction-pad
buttons *and* its new screen-relative ←/→/↑/↓ pad (Phase D) both enable.
Aborts the whole sequence (clearing any partial result) the moment any
step fails to pulse or measure for every camera -- a half-built
calibration is worse than none.

Clicking a direction-pad button solves a duration from that camera's own
calibrated rate for the clicked axis *in the clicked direction*
(`AxisResponse.magnitude_px / duration_ms`, no longer assuming the
opposite direction shares the same rate -- see Phase C above) and a
target distance (`nudge_target_fraction` of this camera's own frame
width), submits that single pulse via `MountTestMoveRunner.submit`, and
reports the resulting displacement the same way a calibration step does
— reusing `_capture`/`_build_response`/`format_response` unchanged.
Clicking a screen-relative pad button instead solves a (possibly
two-axis) pulse sequence via `solve_screen_move` and submits it via
`MountTestMoveRunner.submit_sequence`, deliberately *without* a
synchronous "after" measurement -- see Phase E's own module-docstring
paragraph below for why. A degenerate calibration (AXIS1/AXIS2 responses
too close to parallel to invert) is already refused at `Run Calibration`
time, before any camera's buttons ever enable — this panel never builds a
`CalibrationMatrix` a nudge or screen-move could read a degenerate
response out of.

Real hardware motion with no way to interrupt it once started is a real
safety gap (incident 9551627f) — the "Stop" button, wired to
`OnStepMountPulseAdapter.abort()` (cooperative cancel + OnStep stop) via duck-typing
(`getattr`, not a `MountPort` Protocol method — that Protocol is the
architecture doc's literal contract, not something to extend unilaterally
for one adapter's extra capability), still applies to every pulse this
panel issues. A no-op if the injected `mount` doesn't have `abort()` (e.g.
`NoMountAdapter`/`FakeMountAdapter` unless a test adds one).

Frame capture happens *here*, on the Qt main thread, both before submitting
a pulse (sequence) and again once the runner reports it finished —
deliberately never on the runner's background thread (a real crash was
traced to exactly that: calling `CameraPanel.latest_mono_frame()`
concurrently from a background thread while the same panel's own poll timer
delivers frames on the main thread — see `MountTestMoveRunner`'s docstring).
Detection (`detect_sources`) is fast enough for a single frame that doing it
twice inline on the UI thread doesn't freeze anything, unlike FOV
registration's multi-candidate search.

Every captured before/after frame pair is also kept, raw, in
`diagnostic_frames()` -- real incident de271da5: a pulled diagnostic
bundle's `frames/` only ever held "whatever's currently streaming" (each
panel's own recent-frames buffer), not necessarily the specific pair a
failed calibration/nudge measurement actually used, making the actual
`measure_translation_offset()`/`detect_sources()` failure impossible to
re-run locally without reproducing it live. `MainWindow` folds these into
the same bundle now, labelled (e.g. `"axis1_before_left"`,
`"nudge_after_right"`) so a future incident carries the exact inputs.

Target: "Star"/"Terrestrial" toggle (real user report, incident 6fa2aa59: a
daytime/indoor test correctly refused with "no star detected" -- not a bug,
but there was no way to actually exercise this feature without a real star
in view). "Star" (default, unchanged behavior) measures a point-source
centroid via `detect_sources()`. "Terrestrial" instead cross-correlates the
whole before/after frame via
`astrotool_core.target.translation_offset.measure_translation_offset` --
works against any textured scene, whole-pixel precision only (vs. Star's
sub-pixel centroid). Both modes build the same `AxisResponse` via
`response_from_positions()`; terrestrial mode just calls it with
`(0, 0)` -> `(dx_px, dy_px)` instead of two absolute centroid positions,
since a whole-frame correlation already *is* the displacement, not two
positions to subtract.

Calibration slew duration/rate and each nudge's target size are
`MountAlignmentSettings` (`astrotool_core.config`) — fixed constants sourced
from `~/.CollimationGuideTool/config.toml`'s `[mount_alignment]` table
rather than a runtime UI control (deliberately no way to change them from
this panel).

Issue #27: this panel is the orchestrator across three deliberately
independent layers -- (A) whether a captured frame is even valid to
measure from (`astrotool_core.acquisition.stable_frame_acquisition` for the
exposure-start freshness rule, composed here through
`acquire_verified_frames` over this panel's own two named cameras in
`_capture_both` -- NOT `acquire_settled_frames`, which nothing calls), (B) the
actual pixel-level displacement measurement (`measure_translation_offset`, called only from
`_build_response`, knows nothing about mount motion or timing), and (C)
axis-response/calibration derivation (`astrotool_core.mount.axis_calibration`
-- `response_from_positions`, `is_degenerate`, neither of which touch a
camera or a clock). A failure's real layer is tracked
per camera in `self._last_failure_classes` (`MeasurementFailureClass`,
exposed via `diagnostic_context()`'s `last_failure_classes`) and echoed
into the free-text result label via `_capture_failure_detail()` -- so a
field failure no longer collapses into one generic "calibration failed".

Issue #30: "Tracking is not slewing" -- the measurement mode (issue #48:
derived from the global Operating mode, no local toggle) also determines a
*required mount tracking mode*
(`astrotool_core.mount.tracking_mode.TrackingMode` -- star needs ON,
terrestrial needs OFF), verified/repaired via `_verify_tracking_mode()`
from inside `_capture_both` itself, so it runs before every BEFORE
capture and is re-checked before every AFTER capture too (i.e. right
after whatever commanded movement just finished) with one code path.
`_finish_calibration_step` additionally re-verifies right after *every*
confirmed pulse, including a non-measuring "return" step -- real
confirmation this matters: `MountTestMoveRunner._run()`'s own existing
(#27) `stop_tracking()`-before-every-pulse behavior means tracking gets
disturbed on every single commanded pulse in a calibration sequence, not
just a hypothetical one. A tracking-state precondition failure is its own
`MeasurementFailureClass.TRACKING_STATE_INVALID`, distinct from a
capture/match/calibration-derivation one.

Deeper post-motion image-*stability* verification
(`astrotool_core.acquisition.motion_aware_acquisition`, also issue #30)
is now wired into every before/after capture this panel makes, through
the same single choke point (`_capture_both`, `verify_stability=True` by
default): instead of trusting the first frame whose own exposure
postdates a commanded pulse's completion (issue #27's own bar), a whole
`stability_sample_count`-frame sliding window must measure within
`stability_tolerance_px` of itself, consecutive-pair-to-consecutive-pair,
before either a BEFORE or an AFTER frame is trusted for calibration --
the minimum settle time configured (`frame_settle_ms`) is only ever a
*lower bound*, never proof of stability on its own. A capture that never
stabilizes before its deadline gets its own
`MeasurementFailureClass.IMAGE_NOT_STABLE`, distinct from a frame never
arriving at all (`CAPTURE_INVALID`).

Issue #43: DISPLAY frame != MEASUREMENT frame. There is no exception any
more: a nudge's BEFORE is either the last stability-VERIFIED at-rest
reference (`_reusable_reference`, valid only while the runner has accepted
no sequence since and it is young) or a fresh verified capture -- never
the latest displayed frame. A commanded move whose stable, confident
measurement shows NO image displacement is reported explicitly
(`MeasurementFailureClass.NO_MOTION_DETECTED`), never accepted as evidence.
Every capture leaves a bounded timeline record (`diagnostic_capture_timeline`:
reference, pulse start/end, each frame's captured_at/exposure/exposure start,
stability score/tier) so a bundle can prove overlap or non-overlap. Movement context
(`CommandedMovementContext`) and the stability evidence itself
(`diagnostic_stability_evidence()`) are threaded through for a future
adaptive-settle model this issue explicitly defers building, not
consulted for control flow here.

Issue #31 Phase E: "fast manual positioning must not block on measurement
frames" -- a screen-relative pad click (Phase D, above) is a positioning
action, not a measurement one, so its handler (`_on_screen_move_clicked`/
`_finish_screen_move`) never calls `_capture_both` at all, before *or*
after the pulse -- not even the `verify_stability=True` default nudge's
own "after" capture already uses. This was a deliberate choice, not an
oversight found late: moving that wait to a background thread was
investigated and rejected -- `CameraPanel`'s own `FrameMailbox`
(`astrotool_core.acquisition.stream_controller`) is a single-slot queue,
safe for one consumer, but a second independent consumer (a background
verifier) would either starve the live view of frames or reintroduce the
exact cross-thread camera-state race `MountTestMoveRunner`'s own
docstring already documents a real crash from. Skipping the after-capture
entirely for a pure positioning click sidesteps that risk altogether
without touching threading -- the live view already shows whatever the
move produced, motion-contaminated or not, which is exactly what this
issue's own "DISPLAY FRAME may be motion-contaminated, MEASUREMENT FRAME
must not be" distinction asks for. The existing RA+/RA-/Dec+/Dec- nudge
buttons keep their current measure-and-report behavior unchanged -- this
is a new, additional control, not a replacement.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np
from astrotool_core.acquisition import (
    CommandedMovementContext,
    MotionAwareFrameResult,
    StableFrameWaiter,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import (
    MOUNT_BUSY_REASON,
    WORKER_DECISION_FRESH_WAIT_S,
    AxisDirection,
    AxisResponse,
    CalibrationMatrix,
    MountAxis,
    MountParkPort,
    MountPort,
    TrackingMode,
    TrackingVerificationStatus,
    ensure_tracking_mode,
)
from astrotool_core.mount.movement_sizing import (
    CameraGeometry,
)
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer
from astrotool_core.timing import SYSTEM_CLOCK, Clock
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.application.mount_align_calibration import CalibrationResults
from collimation_tool.application.mount_align_capture import (
    CaptureJob,
    CaptureRequest,
    CaptureResult,
    VerifiedReference,
    capture_blocking,
    evaluate_capture,
    frame_acquisition_result,
    fresh_frame_timeout_s,
    reusable_reference,
    verified_capture_timeout_s,
)
from collimation_tool.application.mount_align_failures import (
    MeasurementFailureClass,
    is_no_motion,
)
from collimation_tool.application.mount_align_interface import (
    MountInterfaceState,
    mount_interface_state,
)
from collimation_tool.application.mount_align_measurement import (
    Measurement,
    TargetMode,
    build_response,
)
from collimation_tool.application.mount_align_messages import (
    AXIS_LABELS,
    CAMERA_LABELS,
    bounded_text,
    calibration_progress_text,
    describe_capture_failures,
    missing_label,
    no_motion_text,
    response_dict,
)
from collimation_tool.application.mount_align_motion import (
    submit_axis_move,
    submit_motion_sequence,
)
from collimation_tool.application.mount_align_plan import (
    CALIBRATION_STEPS,
    CalibrationStep,
    CalibrationStepRole,
    MovementSize,
    NudgeMove,
    PendingAction,
    ScreenDirection,
    step_label,
)
from collimation_tool.application.mount_align_positioning import (
    cap_text,
    evaluate_nudge,
    nudge_cap_arcsec,
    plan_nudge,
    plan_screen_move,
    uncalibrated_nudge_arcsec,
)
from collimation_tool.application.mount_align_sizing import (
    CalibrationSizing,
    angular_unit_rate,
    min_angular_arcsec,
    mount_supports_angular,
)
from collimation_tool.ui.flow_layout import FlowLayout
from collimation_tool.ui.mount_test_move_runner import (
    MountTestMoveRunner,
    supports_timed_pulse,
)

#: S6.6a: these value types moved to `collimation_tool.application.mount_align_*`; they stay
#: importable from here (the panel's public API used by MainWindow and the tests).
__all__ = [
    "MeasurementFailureClass",
    "MountInterfaceState",
    "MountTestMovePanel",
    "MovementSize",
    "ScreenDirection",
    "TargetMode",
]

FrameGetter = Callable[[], np.ndarray | None]
#: S6.7: how long an "after" capture waits for a frame that's provably fresh (its own exposure
#: entirely postdates the pulse+settle completion) is the shared fresh-frame-after-motion rule
#: (`astrotool_core.acquisition.fresh_frame_after_motion`: `DEFAULT_FRESH_FRAME_BUDGET`, 2 s
#: floor, two exposures + 1 s) -- a timeout is treated the same as any other missing "after"
#: frame (see _finish_calibration_step/_finish_nudge's own "no frame available" abort path),
#: never silently downgraded to a stale one.











_log = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 250


#: Real request: the direction pad used to be screen-relative
#: (Up/Down/Left/Right), each button composing a move via
#: `compose_screen_move` that could pulse *both* mount axes together
#: whenever the camera is rotated relative to the mount -- unambiguous
#: once calibrated, but the button's own label ("Up") told an operator
#: nothing about which physical axis (or axes) was actually about to
#: move, especially confusing before ever having a feel for how this
#: particular camera sits relative to the mount. Replaced with one
#: button per axis/direction instead -- each pulses exactly the one
#: named axis, so the label itself is never ambiguous. `AXIS_LABELS`
#: above is the same RA/Dec naming already used everywhere else in this
#: module (diagnostic messages, `format_response`'s callers) --
#: `MountAxis.AXIS1` is RA, `MountAxis.AXIS2` is Dec, by convention (not
#: verified against a specific mount's own physical axis assignment).
_NUDGE_BUTTONS: tuple[tuple[str, MountAxis, AxisDirection], ...] = (
    ("RA +", MountAxis.AXIS1, AxisDirection.POSITIVE),
    ("RA -", MountAxis.AXIS1, AxisDirection.NEGATIVE),
    ("Dec +", MountAxis.AXIS2, AxisDirection.POSITIVE),
    ("Dec -", MountAxis.AXIS2, AxisDirection.NEGATIVE),
)




#: Issue #31 Phase D's own per-camera screen-relative pad -- unlike
#: `_NUDGE_BUTTONS` (always available once connected, issue #31's own
#: Phase A bootstrap requirement), these enable only once that camera has
#: a full, non-degenerate 4-direction `CalibrationMatrix` -- see
#: `_update_buttons_enabled`'s own docstring.
_SCREEN_MOVE_BUTTONS: tuple[tuple[str, ScreenDirection], ...] = (
    ("←", ScreenDirection.LEFT),
    ("→", ScreenDirection.RIGHT),
    ("↑", ScreenDirection.UP),
    ("↓", ScreenDirection.DOWN),
)




_SIZE_BUTTONS: tuple[tuple[str, MovementSize], ...] = (
    ("Small", MovementSize.SMALL),
    ("Medium", MovementSize.MEDIUM),
    ("Large", MovementSize.LARGE),
)




class MountTestMovePanel(QWidget):
    def __init__(
        self,
        mount: MountPort,
        *,
        mount_park: MountParkPort,
        get_left_frame: FrameGetter,
        get_right_frame: FrameGetter,
        title: str = "Mount Alignment",
        settings: MountAlignmentSettings | None = None,
        runner: MountTestMoveRunner | None = None,
        set_left_auto_exposure_paused: Callable[[bool], None] | None = None,
        set_right_auto_exposure_paused: Callable[[bool], None] | None = None,
        get_left_exposure_gain: Callable[[], tuple[float, int]] | None = None,
        get_right_exposure_gain: Callable[[], tuple[float, int]] | None = None,
        wait_for_left_frame: StableFrameWaiter | None = None,
        wait_for_right_frame: StableFrameWaiter | None = None,
        tracking_enforcer: TrackingEnforcer | None = None,
        camera_geometry: Callable[[], list[CameraGeometry]] | None = None,
        calibration_distance_m: Callable[[str], float | None] | None = None,
        threaded_captures: bool = False,
        clock: Clock | None = None,
    ) -> None:
        super().__init__()
        self._mount = mount
        #: S6.7 / S3b(c): the capture timing (motion-completed reference, verified-reference
        #: max age, capture-job elapsed time, the stability window's waits) runs on this clock.
        #: Production: the real clock -- the same timebase the cameras stamp frames in and the
        #: runner (default) reports in. Tests pass the FakeClock they give the runner/frames.
        self._clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        #: Issue #49: True runs every stability-verified capture (a wait that can take tens of
        #: seconds with long exposures) on a worker thread and continues on the GUI thread from
        #: the poll timer -- the event loop never blocks. False (default) runs the same request
        #: inline, which keeps synchronous callers and tests unchanged.
        self._threaded_captures = threaded_captures
        #: Issue #46: per-camera sensor size + plate scale, for sizing calibration
        #: moves to ~25% of the frame. None (or unknown optics) => the legacy
        #: fixed `pulse_ms`/`rate_preset` calibration, unchanged.
        self._camera_geometry = camera_geometry
        self._calibration_distance_m = calibration_distance_m
        #: Issue #44/#48: the app-wide tracking policy AND the single owner of the operating
        #: mode -- Mount Align's measurement strategy and tracking need derive from its mode.
        self._tracking_enforcer = tracking_enforcer
        self._mount_park = mount_park
        self._get_left_frame = get_left_frame
        self._get_right_frame = get_right_frame
        #: Real report ("frames in movement are picked on the
        #: calibration, causing guide to fail") -- see
        #: CameraPanel.wait_for_frame_after()'s own docstring. Optional /
        #: falls back to the plain (instant, no freshness guarantee)
        #: getter above so tests and any caller that doesn't wire a real
        #: CameraPanel don't need to supply one.
        # Reads self._get_left_frame/right_frame (not the get_left_frame/
        # get_right_frame constructor params directly) so a caller (or
        # test) that reassigns those instance attributes after
        # construction is honored by the fallback too.
        self._wait_for_left_frame: StableFrameWaiter = wait_for_left_frame or (
            lambda _reference, _timeout: frame_acquisition_result(self._get_left_frame())
        )
        self._wait_for_right_frame: StableFrameWaiter = wait_for_right_frame or (
            lambda _reference, _timeout: frame_acquisition_result(self._get_right_frame())
        )
        #: See diagnostic_camera_state()'s own docstring -- optional /
        #: defaults to "unknown" (None) so tests and any caller that
        #: doesn't wire a real CameraPanel don't need to supply one.
        self._get_left_exposure_gain: Callable[[], tuple[float, int] | None] = (
            get_left_exposure_gain or (lambda: None)
        )
        self._get_right_exposure_gain: Callable[[], tuple[float, int] | None] = (
            get_right_exposure_gain or (lambda: None)
        )
        #: Real incident ca728d27: auto-exposure roughly doubled the
        #: Guide camera's gain between a calibration step's "before" and
        #: "after" capture, pushing "after" into partial saturation --
        #: correlation genuinely found no shift (both frames' actual
        #: content hadn't moved relative to each other under a pure
        #: gain change, but the *non-linear* clipping broke that), which
        #: then made `compose_screen_move`'s degenerate-matrix guard fire
        #: correctly on bad-but-legitimate-looking input. See
        #: `CameraPanel.set_auto_exposure_paused`'s own docstring for the
        #: fix this wires in: exposure/gain held stable for the whole
        #: bracket from a step's "before" capture through its "after"
        #: capture, without pausing frame capture itself (unlike
        #: `set_updates_paused`, which this deliberately does not use
        #: here). Optional / defaults to a no-op so tests and any caller
        #: that doesn't wire a real CameraPanel don't need to supply one.
        self._set_left_auto_exposure_paused = set_left_auto_exposure_paused or (lambda _: None)
        self._set_right_auto_exposure_paused = set_right_auto_exposure_paused or (lambda _: None)
        self._settings = settings if settings is not None else MountAlignmentSettings()
        self._runner = runner if runner is not None else MountTestMoveRunner()
        self._connected = False
        #: See FocuserPanel's identical field for the reasoning -- a caught
        #: connect() failure otherwise only ever lived in the status
        #: label's transient text, invisible to a manual diagnostic capture.
        self._last_connect_error: str | None = None
        self._pending: PendingAction | None = None
        self._calibration_queue: list[CalibrationStep] = []
        #: Issue #42: the exact availability message currently owned by the
        #: status label (so it is cleared when the condition clears, but a real
        #: calibration/result/error text is never stomped).
        self._availability_message: str | None = None
        #: Issue #43: bounded per-capture timeline (reference/pulse/frame timing + stability
        #: evidence) so a pulled bundle can PROVE overlap or non-overlap for every capture,
        #: not only the last one; and the verified at-rest reference for the next nudge.
        self._capture_timeline: deque[dict[str, Any]] = deque(maxlen=48)
        self._last_pulse_timing: dict[str, float | None] | None = None
        self._verified_reference: VerifiedReference | None = None
        #: Issue #49: the capture currently running on a worker thread (threaded mode), the
        #: generation counter that lets Stop discard a stale result, and the recovery evidence.
        self._capture_job: CaptureJob | None = None
        self._capture_generation = 0
        self._last_internal_error: str | None = None
        self._last_cleanup_ok: bool | None = None
        #: Issue #46 sizing state for the CURRENT run (see _init_sizing) -- S6.6a:
        #: `mount_align_sizing.CalibrationSizing`, reading the mount/settings live.
        self._sizing = CalibrationSizing(
            mount=lambda: self._mount, settings=lambda: self._settings
        )
        self._last_motion_paths: tuple[str, ...] = ()
        #: S6.6a: this run's per-camera results (pairs, characterizations, exclusions) --
        #: `mount_align_calibration.CalibrationResults`.
        self._results = CalibrationResults()
        self._calibration: dict[str, CalibrationMatrix] = {}
        self._last_responses: dict[str, AxisResponse] | None = None
        self._last_error: str | None = None
        #: True between _abort_calibration() submitting a stranded return
        #: pulse and that pulse actually finishing -- see that method's
        #: own docstring and _poll()'s use of this flag.
        self._awaiting_stranded_return = False
        #: Per-camera detail from the most recent `_capture_both` call
        #: whose "after" (or "before") wait didn't come back OK -- see
        #: `_capture_failure_detail()`. Short-lived (overwritten every
        #: `_capture_both` call), unlike `_last_failure_classes` below.
        #: Uniformly `MotionAwareFrameResult` regardless of whether the
        #: failure came from the `verify_stability=True` path or the
        #: instant `verify_stability=False` one -- see
        #: `capture_invalid_result()`.
        self._last_capture_failures: dict[str, MotionAwareFrameResult] = {}
        #: Field bundle 1b157b8b: whether the capture that produced
        #: `_last_capture_failures` had a real prior movement to reference
        #: (`request.movement_context is not None`) -- lets
        #: `_capture_failure_detail()` avoid saying "...overlapped the
        #: move" for a "before" capture that never had one commanded yet.
        #: Same short lifecycle as `_last_capture_failures`.
        self._last_capture_had_movement_context = False
        #: Set by `_verify_tracking_mode()` (via `_capture_both`) whenever
        #: the mount's own tracking state didn't match (and couldn't be
        #: repaired into) what the current target mode requires -- see
        #: `_capture_failure_message()`. Short-lived, same lifecycle as
        #: `_last_capture_failures`.
        self._last_tracking_error: str | None = None
        #: Per-camera failure classification for the *current* calibration
        #: attempt or nudge -- see `MeasurementFailureClass` and
        #: `diagnostic_context()`'s own `last_failure_classes`. Reset at
        #: the start of each attempt (`_on_run_calibration_clicked`/
        #: `_on_nudge_clicked`), accumulates across a calibration
        #: sequence's several steps the same way `_calibration_failed_cameras`
        #: does.
        self._last_failure_classes: dict[str, MeasurementFailureClass] = {}
        #: See diagnostic_frames()'s own docstring.
        self._last_diagnostic_frames: dict[str, np.ndarray] = {}
        #: See diagnostic_camera_state()'s own docstring.
        self._last_diagnostic_camera_state: dict[str, tuple[float, int]] = {}
        #: Real diagnostic 93ba361f-18c6-46f6-9a53-fd05be821b01: Guide's
        #: own AXIS2 "before" frame was a stale capture from well before
        #: this run even started (a completely different scene -- an
        #: indoor curtain, not the outdoor view every other frame from
        #: the same run showed), because `_start_next_calibration_step`
        #: captured every step's "before" with no freshness wait at all
        #: (`_capture_both`'s `after_monotonic=None` path) -- correct
        #: reasoning for the *first* step of a fresh sequence (nothing
        #: has moved yet), but not for AXIS2's: AXIS1's own *return* pulse
        #: (real physical motion) had just completed moments earlier, and
        #: nothing waited for the camera stream to catch up before
        #: grabbing "the current frame". Set whenever any commanded pulse
        #: (measured or a return step, calibration or a nudge) actually
        #: completes -- see `_finish_calibration_step`/`_finish_nudge` --
        #: and consulted by every subsequent "before" capture
        #: (`_start_next_calibration_step`/`_on_nudge_clicked`) so it gets
        #: the exact same freshness wait every "after" capture already
        #: has. `None` means nothing has pulsed yet in this panel's
        #: lifetime (or since the start of the current Run Calibration
        #: click, see `_on_run_calibration_clicked`'s own reset) -- the
        #: original instant/no-wait behavior is still correct then.
        self._last_pulse_completed_at: float | None = None
        #: Issue #30: diagnostics-only context about whatever commanded
        #: movement the *next* capture's stability check is settling
        #: from -- set at the same two points `_last_pulse_completed_at`
        #: is (`_finish_calibration_step`/`_finish_nudge`), reset
        #: alongside it in `_on_run_calibration_clicked`. Never consulted
        #: for control flow, only threaded into `acquire_verified_frames`
        #: so `diagnostic_stability_evidence()` can correlate settle
        #: behavior with what actually moved, for a future adaptive-
        #: settle model this issue explicitly defers building.
        self._last_movement_context: CommandedMovementContext | None = None
        #: See diagnostic_stability_evidence()'s own docstring.
        self._last_diagnostic_stability: dict[str, dict[str, Any]] = {}

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")

        # Issue #48: no local Star/Terrestrial selector -- the global Operating mode decides;
        # this read-only line only says what it currently selects.
        self._measurement_mode_label = QLabel("")
        self._measurement_mode_label.setWordWrap(True)
        target_row = QHBoxLayout()
        target_row.addWidget(self._measurement_mode_label, stretch=1)
        self._show_measurement_mode()

        self._run_calibration_button = QPushButton("Run Calibration")
        self._run_calibration_button.clicked.connect(self._on_run_calibration_clicked)
        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self._on_stop)
        # Field report (2026-09-29): this descriptive label (up to ~340px
        # unwrapped) sharing a row with the buttons forced the whole
        # panel's minimum width past the "Mount & Alignment" tab's default
        # splitter viewport, clipping content further down the panel (the
        # "mount interface not available" message) off the visible edge.
        # Word-wrapping it on its own row lets it shrink to fit instead.
        calibration_description_label = QLabel(
            f"Calibration (~{int(self._settings.calibration_target_fraction * 100)}% of frame, "
            f"preset {self._settings.calibration_rate_preset}; "
            f"fallback {self._settings.pulse_ms}ms @ preset {self._settings.rate_preset})"
            if self._camera_geometry is not None
            else f"Calibration ({self._settings.pulse_ms}ms, "
            f"rate preset {self._settings.rate_preset})"
        )
        calibration_description_label.setWordWrap(True)
        calibration_description_row = QHBoxLayout()
        calibration_description_row.addWidget(calibration_description_label, stretch=1)

        # FlowLayout (not a fixed QHBoxLayout split) -- see FlowLayout's
        # own module docstring: a hand-tuned fixed-width split still
        # clipped on the real Pi screen's own font metrics.
        calibration_row = FlowLayout()
        calibration_row.addWidget(self._run_calibration_button)
        calibration_row.addWidget(self._stop_button)

        self._nudge_buttons: dict[str, dict[str, QPushButton]] = {}
        main_pad_row = self._build_direction_pad("left")
        guide_pad_row = self._build_direction_pad("right")

        #: Issue #31 Phase D: shared size selector -- one choice applies
        #: to whichever screen-move button is clicked next, on either
        #: camera (one shared choice, not a per-camera setting).
        self._size_group = QButtonGroup(self)
        self._size_group.setExclusive(True)
        self._size_buttons: dict[MovementSize, QPushButton] = {}
        size_row = FlowLayout()
        size_row.addWidget(QLabel("Move size"))
        for label, size in _SIZE_BUTTONS:
            button = QPushButton(label)
            button.setCheckable(True)
            self._size_group.addButton(button)
            self._size_buttons[size] = button
            size_row.addWidget(button)
        self._size_buttons[MovementSize.SMALL].setChecked(True)  # conservative default

        self._screen_move_buttons: dict[str, dict[ScreenDirection, QPushButton]] = {}
        main_screen_row = self._build_screen_move_pad("left")
        guide_screen_row = self._build_screen_move_pad("right")

        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(target_row)
        layout.addLayout(calibration_description_row)
        layout.addLayout(calibration_row)
        layout.addLayout(main_pad_row)
        layout.addLayout(guide_pad_row)
        layout.addLayout(size_row)
        layout.addLayout(main_screen_row)
        layout.addLayout(guide_screen_row)
        layout.addWidget(self._result_label)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

        self._update_buttons_enabled()

    def _build_direction_pad(self, camera_key: str) -> FlowLayout:
        """One button per `_NUDGE_BUTTONS` entry -- see that constant's own
        docstring for why RA+/RA-/Dec+/Dec- replaced the earlier screen-
        relative Up/Down/Left/Right pad. FlowLayout (not QHBoxLayout): see
        FlowLayout's own module docstring -- a label + 4 buttons was found
        to become the new binding overflow once the previously-touched
        rows stopped being the widest."""
        row = FlowLayout()
        row.addWidget(QLabel(f"{CAMERA_LABELS[camera_key]}:"))
        buttons: dict[str, QPushButton] = {}
        for label, axis, direction in _NUDGE_BUTTONS:
            button = QPushButton(label)
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, key=camera_key, a=axis, d=direction: (
                    self._on_nudge_clicked(key, a, d)
                )
            )
            buttons[label] = button
            row.addWidget(button)
        self._nudge_buttons[camera_key] = buttons
        return row

    def _build_screen_move_pad(self, camera_key: str) -> FlowLayout:
        """Issue #31 Phase D: one button per `_SCREEN_MOVE_BUTTONS` entry
        -- disabled until this camera has a full, non-degenerate
        4-direction `CalibrationMatrix` (see `_update_buttons_enabled`'s
        own docstring), unlike `_build_direction_pad`'s own RA/Dec pad,
        which is this panel's bootstrap control and enables as soon as
        the mount is connected/movable. FlowLayout, same reasoning as
        `_build_direction_pad`."""
        row = FlowLayout()
        row.addWidget(QLabel(f"{CAMERA_LABELS[camera_key]} screen:"))
        buttons: dict[ScreenDirection, QPushButton] = {}
        for label, screen_direction in _SCREEN_MOVE_BUTTONS:
            button = QPushButton(label)
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, key=camera_key, d=screen_direction: (
                    self._on_screen_move_clicked(key, d)
                )
            )
            buttons[screen_direction] = button
            row.addWidget(button)
        self._screen_move_buttons[camera_key] = buttons
        return row

    def _on_toggle_connect(self, checked: bool) -> None:
        if checked:
            try:
                self._mount.connect()
            except Exception as exc:  # noqa: BLE001 -- any failure must be shown, never swallowed
                self._last_connect_error = str(exc)
                _log.warning("MountTestMovePanel: connect failed: %s", exc)
                self._status_label.setText(f"Connect failed — {exc}")
                self._connect_button.blockSignals(True)
                self._connect_button.setChecked(False)
                self._connect_button.blockSignals(False)
                self._update_buttons_enabled()
                return
            self._last_connect_error = None
            self._connected = True
            self._connect_button.setText("Disconnect")
            self._status_label.setText("Connected.")
            self._timer.start()
        else:
            self._timer.stop()
            self._mount.disconnect()
            self._connected = False
            self._connect_button.setText("Connect")
            self._status_label.setText("Not connected.")
        self._update_buttons_enabled()

    def _operating_mode(self) -> OperatingMode:
        """Issue #48: the global mode's single owner (the TrackingEnforcer), read live -- no
        copy that could go stale. Without one (a standalone panel: no app-wide policy wired)
        the fixed legacy default, star measurement; never a user-selectable local state."""
        enforcer = self._tracking_enforcer
        return enforcer.mode if enforcer is not None else OperatingMode.ASTRONOMICAL

    def _target_mode(self) -> TargetMode:
        """The measurement strategy the global mode selects: whole-frame cross-correlation
        against a fixed terrestrial scene, or a star centroid in astronomical mode."""
        return "terrestrial" if self._operating_mode() is OperatingMode.TERRESTRIAL else "star"

    def _show_measurement_mode(self) -> None:
        if self._target_mode() == "terrestrial":
            text = "Measurement: terrestrial scene (whole-frame correlation, tracking off)"
        else:
            text = "Measurement: star centroid (astronomical, tracking on)"
        self._measurement_mode_label.setText(f"{text} -- follows the Operating mode")

    def operating_mode_changed(self) -> None:
        """Issue #48: the global mode changed. Mount Align follows it at once; a calibration or
        move still running was measured under the previous mode (strategy and tracking) and is
        stopped rather than finished with mixed evidence."""
        self._show_measurement_mode()
        running = (
            bool(self._calibration_queue)
            or self._pending is not None
            or self._capture_job is not None
            or self._runner.is_busy
        )
        if not running:
            return
        self._on_stop()
        self._last_error = "operating mode changed during the run"
        # Keep Stop's own notes (net commanded offset; controls returning once the interrupted
        # move call has ended) -- only the cause differs from a user Stop.
        detail = self._result_label.text().removeprefix("Stopped by the user")
        self._result_label.setText(
            f"Stopped: the operating mode changed to {self._operating_mode().value} during the "
            f"run{detail} Its measurements are discarded; run it again."
        )

    def _required_tracking_mode(self) -> TrackingMode:
        """Issue #30's "Tracking is not slewing": star calibration
        requires tracking ON (so it can measure real astronomical
        motion), terrestrial requires tracking OFF (tracking would
        otherwise add deliberate continuous image motion on top of
        whatever the calibration pulse itself produces). Issue #48: the
        tracking policy decides it from the global mode alone."""
        enforcer = self._tracking_enforcer
        if enforcer is not None:
            return enforcer.measurement_tracking()
        return TrackingMode.OFF if self._target_mode() == "terrestrial" else TrackingMode.ON

    def _tracking_failure_message(
        self, required: TrackingMode, mode: TargetMode, *, fresh_wait_s: float = 0.0
    ) -> str | None:
        """Verifies (and, if needed, repairs) the mount's tracking state against `required` --
        None if OK, else a human-readable failure message. Side-effect free with respect to the
        panel (issue #49: it runs on the capture worker thread); callers that record failure
        classes do so themselves (`_verify_tracking_mode`, `_apply_capture`). `fresh_wait_s`:
        0 on the GUI thread; the capture worker passes `WORKER_DECISION_FRESH_WAIT_S` (S6.0c
        re-review R1: a momentary GUI poll on the shared connection is not "mount busy")."""
        enforcer = self._tracking_enforcer
        status = self._mount_park.status()
        # S6.0c: a busy mount (no fresh reading) is not "no mount" -- the enforcer denies it.
        if enforcer is not None and (status.available or not status.fresh):
            gate = enforcer.verify(required, "mount_align", fresh_wait_s=fresh_wait_s)
            if gate.allowed:
                return None
            return (
                f"mount tracking must be {required.value} for {mode} "
                f"calibration -- {gate.reason}"
            )
        result = ensure_tracking_mode(self._mount_park, required, fresh_wait_s=fresh_wait_s)
        if result.ok:
            return None
        if result.status is TrackingVerificationStatus.BUSY:
            return f"mount tracking must be {required.value} for {mode} calibration -- " + (
                MOUNT_BUSY_REASON
            )
        observed = result.observed_mode.value if result.observed_mode is not None else "unavailable"
        return (
            f"mount tracking must be {required.value} for {mode} calibration "
            f"(currently {observed}, {result.status.value})"
        )

    def _verify_tracking_mode(self) -> str | None:
        """Verifies (and, if needed, repairs) the mount's tracking state against
        `_required_tracking_mode()` -- returns `None` if OK, or a human-readable failure message
        otherwise. Runs before every BEFORE capture and again before every AFTER capture (issue
        #30: "After each commanded slew/pulse, tracking state shall be re-verified because the
        driver/mount may change state as a side effect")."""
        message = self._tracking_failure_message(
            self._required_tracking_mode(), self._target_mode()
        )
        if message is not None:
            self._last_failure_classes["left"] = MeasurementFailureClass.TRACKING_STATE_INVALID
            self._last_failure_classes["right"] = MeasurementFailureClass.TRACKING_STATE_INVALID
        return message

    def _missing_label(self, mode: TargetMode) -> str:
        return missing_label(mode)

    def _fresh_frame_timeout_s(self) -> float:
        """How long `_capture_both`'s freshness waits may take, scaled for the cameras' current
        exposure (S6.6a: the rule lives in `mount_align_capture.fresh_frame_timeout_s`)."""
        return fresh_frame_timeout_s(
            self._get_left_exposure_gain(), self._get_right_exposure_gain()
        )

    def _verified_capture_timeout_s(self) -> float:
        """Total deadline for one `verify_stability=True` capture (see
        `mount_align_capture.verified_capture_timeout_s`)."""
        return verified_capture_timeout_s(self._settings, self._fresh_frame_timeout_s())

    def _capture_both(
        self,
        mode: TargetMode,
        *,
        diagnostic_label: str | None = None,
        after_monotonic: float | None = None,
        verify_stability: bool = True,
    ) -> dict[str, Measurement] | None:
        """Capture both cameras' "before"/"after" measurement in one shot,
        or None if either is missing -- the caller decides how to phrase
        the resulting error message (calibration-step vs. nudge).

        `diagnostic_label`, when given, also stashes the *raw* frame each
        camera returned (before `_capture()`'s mode-specific reduction to
        a centroid or a bare array-for-correlation) into
        `self._last_diagnostic_frames`, so `diagnostic_frames()` always
        has the actual frame pair a measurement was just taken from --
        see that method's own docstring for why (real incident
        de271da5: a pulled bundle's frames were "whatever's currently
        streaming", not necessarily what a failed measurement actually
        used).

        `after_monotonic`, when given, blocks for a frame provably
        captured after that time instead of an instant (possibly stale
        or mid-motion) read -- see CameraPanel.wait_for_frame_after()'s
        own docstring. Every "after" capture of a calibration step/nudge
        passes this (the pulse that just ran). A "before" capture passes
        it too whenever `self._last_pulse_completed_at` is set -- i.e.
        whenever *anything* has already pulsed this mount before now
        (real diagnostic 93ba361f-18c6-46f6-9a53-fd05be821b01: AXIS2's
        own "before" capture used to grab an instant, unwaited read even
        though AXIS1's own *return* pulse had physically moved the mount
        moments earlier, and returned a genuinely stale frame from well
        before this run even started -- a completely different real-world
        scene, not merely an old exposure of the same one). Only the very
        first "before" capture of a fresh sequence (nothing has moved yet
        in this attempt) still passes `None` here.

        Real report ("still 2-3 frames are shown showing movement" after
        a pulse): a single frame delivered past `after_monotonic` isn't
        strong enough evidence the mount has actually finished
        mechanically settling -- residual vibration/backlash damping out
        can outlast both `settle_ms` and that very first fresh-delivered
        frame. User's own recipe: "check on mount being stopped first,
        grant the [frame_settle_ms] and take frame then only" -- so this
        first confirms the stream has caught up past the pulse on *both*
        cameras (the "mount stopped" check, from the video pipeline's own
        point of view), then grants `frame_settle_ms` again, and only
        *then* takes the frame actually used for measurement -- from
        *that* later point, not the first barely-fresh one.

        Issue #27: the underlying exposure-timing wait -- "is this frame's
        own exposure provably past the reference?" -- lives in
        `astrotool_core.acquisition.stable_frame_acquisition` (the "A"
        layer, camera-count-independent by construction).

        Issue #30: by default (`verify_stability=True`), every capture --
        including this run's very first "before", using "now" as its own
        reference when there's no prior pulse to settle from -- also goes
        through `astrotool_core.acquisition.motion_aware_acquisition.
        acquire_verified_frames`'s own stability-verified policy layered
        on top of that same exposure-timing wait: a whole
        `stability_sample_count`-frame sliding window must measure within
        `stability_tolerance_px` of itself before a frame is trusted, not
        just the first one whose own exposure happens to postdate the
        reference. (`verify_stability=False`, the instant/no-wait read, has no
        production caller left -- issue #43.) This method stashes per-camera failure detail in
        `self._last_capture_failures` for `_capture_failure_detail()`,
        and (only for the verified path) stability evidence in
        `self._last_diagnostic_stability` for
        `diagnostic_stability_evidence()`.

        Issue #30: checks/repairs the mount's own tracking state (see
        `_verify_tracking_mode`) before attempting either capture --
        called from here rather than scattered across every "before"/
        "after" call site covers both "verified before BEFORE capture"
        and "re-verified after commanded movement" with one check, since
        this method IS the thing every before/after capture goes
        through."""
        request = self._snapshot_capture_request(
            mode, diagnostic_label, after_monotonic, verify_stability
        )
        return self._apply_capture(self._capture_blocking(request))

    # ---- issue #49: the capture split into worker-safe and GUI-thread halves ------------
    def _snapshot_capture_request(
        self,
        mode: TargetMode,
        label: str | None,
        after_monotonic: float | None,
        verify_stability: bool,
    ) -> CaptureRequest:
        """GUI thread: every widget/GUI-state read the capture needs."""
        return CaptureRequest(
            mode=mode,
            label=label,
            after_monotonic=after_monotonic,
            verify_stability=verify_stability,
            timeout_s=self._verified_capture_timeout_s(),
            required_tracking=self._required_tracking_mode(),
            movement_context=self._last_movement_context,
            left_state=self._get_left_exposure_gain(),
            right_state=self._get_right_exposure_gain(),
        )

    def _capture_blocking(
        self,
        request: CaptureRequest,
        cancelled: Callable[[], bool] | None = None,
        *,
        fresh_wait_s: float = 0.0,
    ) -> CaptureResult:
        """The slow part -- `mount_align_capture.capture_blocking` over this panel's current
        ports. Touches no widget and mutates no panel state, so it is safe on a worker thread
        (which passes a `fresh_wait_s`, see `_tracking_failure_message`)."""
        return capture_blocking(
            request,
            tracking_failure=lambda required, mode: self._tracking_failure_message(
                required, mode, fresh_wait_s=fresh_wait_s
            ),
            get_left_frame=self._get_left_frame,
            get_right_frame=self._get_right_frame,
            wait_for_left_frame=self._wait_for_left_frame,
            wait_for_right_frame=self._wait_for_right_frame,
            settings=self._settings,
            clock=self._clock,
            cancelled=cancelled,
        )

    def _apply_capture(self, result: CaptureResult) -> dict[str, Measurement] | None:
        """GUI thread: store the bookkeeping the capture leaves behind (failure classes,
        stability evidence, timeline, diagnostic frames, verified reference -- classified by
        `mount_align_capture.evaluate_capture`) and return the measurements."""
        outcome = evaluate_capture(result, pulse_timing=self._last_pulse_timing)
        self._last_capture_failures = outcome.failures
        self._last_capture_had_movement_context = outcome.had_movement_context
        self._last_diagnostic_stability = outcome.stability
        self._last_tracking_error = outcome.tracking_error
        self._last_failure_classes.update(outcome.failure_classes)
        if outcome.timeline_entry is not None:
            self._capture_timeline.append(outcome.timeline_entry)
        self._last_diagnostic_frames.update(outcome.diagnostic_frames)
        self._last_diagnostic_camera_state.update(outcome.diagnostic_camera_state)
        if outcome.verified and outcome.measurements is not None:
            self._verified_reference = VerifiedReference(
                taken_at=self._clock.monotonic(),
                submit_count=getattr(self._runner, "submit_count", None),
                mode=result.request.mode,
                measurements=dict(outcome.measurements),
            )
        return outcome.measurements

    def _reusable_reference(self, mode: TargetMode) -> dict[str, Measurement] | None:
        """Issue #43: see `mount_align_capture.reusable_reference`."""
        return reusable_reference(
            self._verified_reference,
            mode=mode,
            submit_count=getattr(self._runner, "submit_count", None),
            now=self._clock.monotonic(),
        )

    def _request_capture(
        self,
        mode: TargetMode,
        *,
        label: str,
        after_monotonic: float | None,
        then: Callable[[dict[str, Measurement] | None], None],
    ) -> None:
        """Run one both-camera verified capture and hand its result to `then` (issue #49).

        Inline (default): the capture runs here and `then` is called at once. Threaded: the
        capture runs on a worker thread; `_poll` (the panel's QTimer, on the GUI thread) applies
        its result and calls `then` -- the event loop is never blocked by a frame wait, the
        panel shows live progress, and Stop can abandon the job at any time."""
        if not self._threaded_captures:
            then(self._capture_both(mode, diagnostic_label=label, after_monotonic=after_monotonic))
            return
        request = self._snapshot_capture_request(mode, label, after_monotonic, True)
        self._capture_generation += 1
        job = CaptureJob(
            self._capture_generation, label, request, then, self._clock.monotonic()
        )
        self._capture_job = job
        self._show_capture_progress(job)
        threading.Thread(
            target=self._run_capture_job, args=(job,), daemon=True, name="mount-align-capture"
        ).start()
        self._update_buttons_enabled()

    def _run_capture_job(self, job: CaptureJob) -> None:
        try:
            job.result = self._capture_blocking(
                job.request, job.cancel.is_set, fresh_wait_s=WORKER_DECISION_FRESH_WAIT_S
            )
        except BaseException as exc:  # noqa: BLE001 -- reported on the GUI thread by _poll
            job.error = exc
        finally:
            job.done.set()

    def _show_capture_progress(self, job: CaptureJob) -> None:
        elapsed = self._clock.monotonic() - job.started_at
        what = job.label.replace("_", " ")
        self._result_label.setText(
            f"Capturing {what} -- waiting for fresh, stable frames from both cameras "
            f"({elapsed:.1f} s of up to {job.request.timeout_s:.0f} s)…"
        )

    def _poll_capture_job(self, job: CaptureJob) -> None:
        """GUI thread: progress while the worker runs; on completion apply + continue."""
        if not job.done.is_set():
            self._show_capture_progress(job)
            return
        self._capture_job = None
        if job.error is not None:
            _log.error("Mount Align capture worker failed", exc_info=job.error)
            self._fail_safe(f"capture failed: {job.error!r}", job.error)
            return
        assert job.result is not None
        job.then(self._apply_capture(job.result))

    # ---- issue #49: one recovery path for every failure ----------------------------------
    def is_idle(self) -> bool:
        """True when nothing is in flight: no capture, no mount sequence, no pending step."""
        return (
            self._capture_job is None
            and not self._runner.is_busy
            and not self._calibration_queue
            and self._pending is None
            and not self._awaiting_stranded_return
        )

    def _cancel_activity(self) -> None:
        """Drop whatever this panel has in flight (queue, pending step, capture job) and let go
        of the cameras' auto-exposure -- the shared reset behind Stop and `_fail_safe`."""
        self._calibration_queue = []
        self._pending = None
        self._awaiting_stranded_return = False
        job = self._capture_job
        if job is not None:
            job.cancel.set()  # the abandoned worker only READS frames; its result is discarded
            self._capture_job = None
        self._capture_generation += 1
        self._resume_auto_exposure()

    def _fail_safe(self, reason: str, exc: BaseException | None = None) -> None:
        """Terminal failure of the panel's own machinery (an unexpected exception): reset to an
        idle, retryable state, keep the evidence, show the reason, re-enable controls."""
        self._last_cleanup_ok = False
        try:
            self._cancel_activity()
            self._last_error = reason
            self._last_internal_error = (
                f"{reason}\n{''.join(traceback.format_exception(exc))}" if exc else reason
            )
            self._result_label.setText(f"Failed: {reason}")
            self._update_buttons_enabled()
            self._last_cleanup_ok = True
        except Exception:  # noqa: BLE001 -- cleanup must never raise into the event loop
            _log.exception("Mount Align fail-safe cleanup itself failed")

    def diagnostic_activity(self) -> dict[str, Any]:
        """Issue #49: what the panel is doing right now, for a bundle taken during an apparent
        freeze -- distinguishes a blocked loop from a long capture from a dead worker."""
        job = self._capture_job
        if job is not None:
            state = "capturing"
        elif self._runner.is_busy:
            state = "moving"
        elif self._calibration_queue or self._pending is not None:
            state = "calibrating"
        else:
            state = "idle"
        return {
            "state": state,
            "threaded_captures": self._threaded_captures,
            "capture_in_flight": job is not None,
            "capture_label": job.label if job is not None else None,
            "capture_elapsed_s": (
                (self._clock.monotonic() - job.started_at) if job is not None else 0.0
            ),
            "runner_busy": self._runner.is_busy,
            "poll_timer_active": self._timer.isActive(),
            "calibration_queue_len": len(self._calibration_queue),
            "last_cleanup_ok": self._last_cleanup_ok,
            "last_internal_error": self._last_internal_error,
        }

    def _capture_failure_detail(self) -> str:
        """The per-camera note for the most recent capture's failures (issue #27/#30, field
        bundle 1b157b8b) -- see `mount_align_messages.describe_capture_failures`."""
        return describe_capture_failures(
            self._last_capture_failures,
            had_movement_context=self._last_capture_had_movement_context,
        )

    def _capture_failure_message(self, generic: str) -> str:
        """The message to show for a `_capture_both` call that just
        returned `None` -- `generic` is the existing missing-frame
        wording (e.g. `"no star detected before pulsing"`). Issue #30: a
        tracking-state precondition failure gets its own specific message
        (set by `_verify_tracking_mode`) instead of being reported as a
        generic missing-frame one, which would be actively misleading --
        no frame was ever attempted in that case."""
        if self._last_tracking_error is not None:
            return self._last_tracking_error
        return f"{generic}{self._capture_failure_detail()}"

    def diagnostic_frames(self) -> dict[str, np.ndarray]:
        """The raw before/after frame pair(s) from the most recent
        calibration step(s) and/or nudge -- labelled e.g.
        `"axis1_before_left"`, `"nudge_after_right"`. `MainWindow`'s own
        diagnostic capture folds these into the saved bundle's `frames/`
        alongside the regular per-camera recent-frames, so a failed
        measurement can be re-run directly against
        `measure_translation_offset()`/`detect_sources()` from a pulled
        bundle without needing to reproduce it live."""
        return dict(self._last_diagnostic_frames)

    def diagnostic_camera_state(self) -> dict[str, tuple[float, int]]:
        """The (exposure_ms, gain) each `diagnostic_frames()` entry was
        actually captured with, keyed the same way -- e.g. real incidents
        ca728d27/0de26787, where whether auto-exposure changed gain
        *between* a step's "before" and "after" capture kept being the
        open question a diagnostic bundle couldn't actually answer (the
        saved frames alone don't carry the camera settings they were
        taken with). `MainWindow`'s own diagnostic capture writes these
        into each frame's own FITS header, so a future bundle settles it
        directly instead of guessing. A label is only present here if the
        camera that produced it had a `get_*_exposure_gain` callable
        wired (optional -- see constructor)."""
        return dict(self._last_diagnostic_camera_state)

    def diagnostic_stability_evidence(self) -> dict[str, dict[str, Any]]:
        """Issue #30's own diagnostics ask: per-camera evidence from the
        most recent `verify_stability=True` capture -- `{"status":
        ..., "stability_status": ..., "max_displacement_px": ...,
        "samples_checked": ..., ...CommandedMovementContext fields}`,
        keyed "left"/"right" (never populated by a nudge's own instant
        "before" capture, which skips the stability layer entirely --
        see `_capture_both`'s own docstring). This is what would have let
        a pulled diagnostic bundle answer "did the stability check even
        run, and what did it measure" for a real incident like
        `859f2520` -- something no bundle pulled before this wiring could
        show at all."""
        return {key: dict(evidence) for key, evidence in self._last_diagnostic_stability.items()}

    def _pause_auto_exposure(self) -> None:
        self._set_left_auto_exposure_paused(True)
        self._set_right_auto_exposure_paused(True)

    def _resume_auto_exposure(self) -> None:
        self._set_left_auto_exposure_paused(False)
        self._set_right_auto_exposure_paused(False)

    def _on_run_calibration_clicked(self) -> None:
        try:
            self._begin_calibration()
        except Exception as exc:
            self._fail_safe(f"could not start calibration: {exc}", exc)
            raise

    def _begin_calibration(self) -> None:
        self._calibration_queue = list(CALIBRATION_STEPS)
        self._results.reset()
        self._sizing.reset_net()
        self._last_failure_classes = {}
        self._init_sizing()
        # Real diagnostic 93ba361f: reset so this run's own very first
        # "before" capture uses "now" as its own stability-check reference
        # (see _last_pulse_completed_at's own docstring) -- nothing has
        # moved yet in *this* attempt, even if an earlier nudge or
        # calibration run left a stale (but harmless to reuse as a
        # reference) old timestamp behind. _last_movement_context resets
        # alongside it -- see that attribute's own docstring.
        self._last_pulse_completed_at = None
        self._last_movement_context = None
        # Paused for the whole 8-step sequence, not just per-step -- see
        # the constructor's own docstring on _set_left/right_auto_exposure_paused.
        # Resumed in _abort_calibration (every failure path) and
        # _finish_calibration (success).
        self._pause_auto_exposure()
        self._start_next_calibration_step()

    def _start_next_calibration_step(self) -> None:
        if not self._calibration_queue:
            self._finish_calibration()
            self._update_buttons_enabled()
            return
        step = self._calibration_queue[0]
        mode = self._target_mode()
        duration_ms = self._sizing.step_duration_ms(step)
        label = step_label(step, "before")
        # Real diagnostic 93ba361f: `_last_pulse_completed_at` is None
        # only for this sequence's very first step (see
        # _on_run_calibration_clicked's own reset) -- every later one
        # gets the same freshness wait its own "after" already has,
        # since the *previous* step's own pulse just moved the mount.
        # See _last_pulse_completed_at's own docstring.
        if step.role is CalibrationStepRole.RETURN:
            # unmeasured corrective pulse: nothing to capture first
            self._submit_calibration_step(step, mode, duration_ms, {})
            return
        self._request_capture(
            mode,
            label=label,
            after_monotonic=self._last_pulse_completed_at,
            then=lambda captured: self._after_before_capture(step, mode, duration_ms, captured),
        )

    def _after_before_capture(
        self,
        step: CalibrationStep,
        mode: TargetMode,
        duration_ms: int,
        captured: dict[str, Measurement] | None,
    ) -> None:
        if not captured:
            self._abort_calibration(
                self._capture_failure_message(f"{self._missing_label(mode)} before pulsing"),
                axis=step.axis,
            )
            return
        self._submit_calibration_step(step, mode, duration_ms, captured)

    def _submit_calibration_step(
        self,
        step: CalibrationStep,
        mode: TargetMode,
        duration_ms: int,
        before: dict[str, Measurement],
    ) -> None:
        arcsec = self._sizing.step_arcsec(step, duration_ms)
        started = submit_axis_move(
            self._runner,
            self._mount_park,
            self._mount,
            step.axis,
            step.direction,
            duration_ms,
            arcsec,
            rate_preset=self._sizing.rate_preset,
            settle_ms=self._settings.settle_ms,
        )
        if not started:
            self._abort_calibration(
                "mount busy — could not start calibration pulse", axis=step.axis
            )
            return
        self._calibration_queue.pop(0)
        self._pending = PendingAction(
            kind="calibration",
            before=before,
            mode=mode,
            step=step,
            duration_ms=duration_ms,
            angular_arcsec=arcsec,
        )
        if step.role is CalibrationStepRole.RETURN:
            self._result_label.setText("Returning the mount to its start position…")
        else:
            self._result_label.setText(
                calibration_progress_text(
                    step.axis, step.direction.name, step.role.value, duration_ms, arcsec
                )
            )
        self._update_buttons_enabled()

    def _abort_calibration(self, message: str, *, axis: MountAxis) -> None:
        """Stop the calibration sequence and report `message`.

        `axis`: real incident a082144a, adapted for issue #31 Phase C's
        own all-measured backlash sequence (see `CALIBRATION_STEPS`'s
        own docstring) -- there's no longer a single unmeasured "return"
        pulse immediately following each measured one to strand and fire
        fire-and-forget; a mid-group abort can instead leave several
        same-direction pulses uncancelled. `self._sizing.axis_net_pulse_ms[axis]`
        tracks `axis`'s own net commanded displacement so far this run
        (updated in `_finish_calibration_step` right alongside
        `_last_pulse_completed_at`); if nonzero, ONE corrective pulse --
        the opposite direction, sized to cancel exactly that net -- is
        submitted fire-and-forget before the queue clears, the same
        "don't strand the mount" intent the old single-return-step trick
        had. Nothing further in this calibration attempt depends on the
        correction's own outcome, and `_poll()` already tolerates a
        completion with no matching `_pending` (the same shape as any
        other untracked pulse).

        Real report ("calibration failed is stated already while mount
        is moving"): this used to show the final "Calibration failed"
        text immediately, then submit the stranded return pulse right
        after -- so the mount kept visibly moving for several more
        seconds under a message that already read as final/settled.
        When a correction is actually submitted, the message now says so
        explicitly, and `_poll()` (via `_awaiting_stranded_return`)
        updates it once that correction actually finishes -- to a
        confirmation if it succeeded, or an explicit warning if it
        didn't (real pulses can still fail -- see MountTestMoveRunner's
        own retry logic, which already covers transient rejection but
        not every possible failure).
        """
        # Issue #43/#46: capped return chunks (see `CalibrationSizing.abort_return`).
        motion, suffix = self._sizing.abort_return(
            axis, self._settings.max_calibration_pulse_ms
        )
        self._calibration_queue = []
        self._pending = None
        self._last_error = message
        self._result_label.setText(f"Calibration failed: {message}{suffix}")
        self._resume_auto_exposure()
        if motion:
            self._awaiting_stranded_return = True
            submit_motion_sequence(
                self._runner,
                self._mount_park,
                self._mount,
                motion,
                rate_preset=self._sizing.rate_preset,
                settle_ms=self._settings.settle_ms,
            )
        self._update_buttons_enabled()

    def _finish_calibration_step(
        self,
        pending: PendingAction,
        *,
        pulsed: bool,
        pulse_error: str | None,
        completed_at: float,
    ) -> None:
        """Real report (diagnostic d14c3a9b): "esp. guide cam right...
        right movement greyed out". Guide's own frames were fine (real,
        high-confidence structure) that whole run -- Main's weren't (gain
        escalated to near its own max, producing a frame that's mostly
        amplified sensor noise). The old all-or-nothing behavior here
        aborted the *entire* calibration the moment either camera's
        measurement failed, discarding Guide's own already-good reading
        along with Main's bad one -- Guide's nudge buttons stayed
        disabled for a problem that was really only ever Main's.

        A camera whose own measurement fails now gets excluded
        (`_calibration_failed_cameras`) rather than aborting the whole
        sequence: the shared mount pulses keep running regardless (one
        physical mount serves both cameras, so there's no reason to stop
        moving it just because one camera's own signal is bad), and
        whichever camera(s) keep succeeding still get a complete,
        usable calibration. Only once *both* cameras have failed is
        there nothing left to gain, and the sequence actually aborts
        (still via `_abort_calibration`'s own corrective pulse, same as
        any other abort after a successful pulse).

        Deliberately unchanged: a missing raw frame entirely (not a
        measured-but-too-noisy one) still aborts everything immediately,
        both here and in `_start_next_calibration_step`'s own "before"
        capture -- a camera with literally no frame at all is a more
        fundamental streaming/connection problem, not a signal-quality
        one, and not what this incident was about.
        """
        step = pending.step
        assert step is not None
        if not pulsed:
            self._abort_calibration(pulse_error or "pulse failed", axis=step.axis)
            return
        # Issue #31: every step is a real, measured pulse now (see
        # CALIBRATION_STEPS's own docstring) -- tracks this axis's own
        # net commanded displacement so _abort_calibration can correct it
        # if this run gets aborted before the axis's own 4-step group
        # completes (which always cancels this back to exactly 0).
        duration_ms = pending.duration_ms
        self._sizing.account_arcsec(step, pending.duration_ms, pending.angular_arcsec)
        self._sizing.account_pulse(step, duration_ms)
        # Real diagnostic 93ba361f: recorded for every completed pulse --
        # see _last_pulse_completed_at's own docstring -- so the *next*
        # step's own "before" capture knows to wait for the stream to
        # catch up past this motion instead of grabbing whatever's
        # already cached.
        self._last_pulse_completed_at = completed_at
        self._last_movement_context = CommandedMovementContext(
            movement_type=pending.kind,
            duration_ms=duration_ms,
            rate_preset=self._sizing.rate_preset,
            axis=step.axis.name,
            direction=step.direction.name,
            minimum_settle_ms=self._settings.frame_settle_ms,
        )
        # Issue #30: tracking state must be re-verified after *every*
        # commanded pulse -- the same real OnStep quirk that motivated
        # this is a side effect of any park/unpark-adjacent
        # command, not specifically a measured one. `_capture_both`
        # (below) re-checks this again on its own -- redundant but
        # harmless.
        tracking_error = self._verify_tracking_mode()
        if tracking_error is not None:
            self._abort_calibration(tracking_error, axis=step.axis)
            return
        if step.role is CalibrationStepRole.RETURN:
            self._start_next_calibration_step()  # unmeasured: no AFTER capture
            return
        label = step_label(step, "after")
        self._request_capture(
            pending.mode,
            label=label,
            after_monotonic=completed_at,
            then=lambda after: self._after_after_capture(step, pending, duration_ms, after),
        )

    def _after_after_capture(
        self,
        step: CalibrationStep,
        pending: PendingAction,
        duration_ms: int,
        after: dict[str, Measurement] | None,
    ) -> None:
        if not after:
            self._abort_calibration(
                self._capture_failure_message(
                    f"{self._missing_label(pending.mode)} after the move"
                ),
                axis=step.axis,
            )
            return
        responses, newly_failed = self._measure_step_responses(step, pending, after, duration_ms)
        self._sizing.log_motion(
            step,
            angular_arcsec=pending.angular_arcsec,
            duration_ms=pending.duration_ms,
            motion_paths=self._last_motion_paths,
        )
        self._sizing.learn_rate(
            step,
            angular_arcsec=pending.angular_arcsec,
            duration_ms=pending.duration_ms,
            responses=responses,
        )
        if len(self._calibration_failed_cameras) >= 2:
            self._abort_calibration(
                "not enough structure to measure a displacement in either camera",
                axis=step.axis,
            )
            return
        if self._sizing.probe(
            step,
            duration_ms,
            responses,
            exclude_no_motion=self._exclude_camera_no_motion,
            exclude_bounded=self._exclude_camera_bounded,
        ):
            # issue #46: probe discarded, resized, re-queued
            self._calibration_queue.insert(0, step)
            self._start_next_calibration_step()
            return
        for key, response in responses.items():
            self._results.record_direction_response(key, step, response)
        if newly_failed:
            self._last_error = (
                f"not enough structure to measure a displacement in: "
                f"{', '.join(newly_failed)} -- excluded from this calibration"
            )
        elif responses:
            self._last_error = None
        self._last_responses = responses or self._last_responses
        self._calibration_queue[0:0] = self._sizing.axis_group_followups(
            step,
            failed_cameras=self._calibration_failed_cameras,
            partial=self._calibration_partial,
            exclude_no_motion=self._exclude_camera_no_motion,
            exclude_bounded=self._exclude_camera_bounded,
        )
        self._start_next_calibration_step()

    def _measure_step_responses(
        self,
        step: CalibrationStep,
        pending: PendingAction,
        after: dict[str, Measurement],
        duration_ms: int,
    ) -> tuple[dict[str, AxisResponse], list[str]]:
        """Per-camera measurement of one calibration step (issue #43: a confident zero is
        never accepted as evidence) -- `CalibrationResults.measure_step`. Returns (responses,
        newly excluded cameras)."""
        responses, newly_failed, match_failed = self._results.measure_step(
            step,
            pending.before,
            after,
            lambda _key, before, after_: self._build_response(
                pending.mode, step.axis, step.direction, duration_ms, before, after_
            ),
        )
        for key in match_failed:
            self._last_failure_classes[key] = MeasurementFailureClass.MATCH_FAILED
        return responses, newly_failed

    # ---- issue #43: 'the image did not move' is an explicit outcome -------------
    def _is_no_motion(self, response: AxisResponse, measurement: Measurement) -> bool:
        return is_no_motion(response, measurement)

    def _no_motion_text(self, duration_ms: int) -> str:
        return no_motion_text(duration_ms, self._sizing.rate_preset)

    def _exclude_camera_no_motion(self, key: str, duration_ms: int) -> None:
        self._calibration_failed_cameras.add(key)
        self._last_failure_classes[key] = MeasurementFailureClass.NO_MOTION_DETECTED
        self._sizing.bounded[key] = self._no_motion_text(duration_ms)
        self._sizing.log.append({"event": "no_motion", "camera": key, "duration_ms": duration_ms})

    # ---- issue #46: calibration move sizing ------------------------------
    def _init_sizing(self) -> None:
        """Decide (per run) whether moves are sized from the cameras' optics -- see
        `mount_align_sizing.CalibrationSizing.start`."""
        cameras = self._camera_geometry() if self._camera_geometry is not None else None
        self._sizing.start(
            angular_only=self._mount_is_angular_only(),
            cameras=cameras,
            distance_m=(
                self._sizing_distance_for(self._target_mode()) if cameras is not None else None
            ),
        )

    # S6.6a: the sizing state lives in `self._sizing`; these names stay readable for diagnostics
    # and the existing tests.
    @property
    def _sizing_log(self) -> list[dict[str, Any]]:
        return self._sizing.log

    @property
    def _angular_active(self) -> bool:
        return self._sizing.angular_active

    @property
    def _calibration_rate_preset(self) -> str | None:
        return self._sizing.rate_preset

    @property
    def _axis_net_pulse_ms(self) -> dict[MountAxis, int]:
        return self._sizing.axis_net_pulse_ms

    def _mount_supports_angular(self) -> bool:
        return mount_supports_angular(self._mount)

    def _mount_is_angular_only(self) -> bool:
        """S6.0: angular moves exist but timed pulses do not (the adapter's own reported
        capability -- see `supports_timed_pulse`, the single authoritative reader)."""
        return self._mount_supports_angular() and not supports_timed_pulse(self._mount)

    def _angular_unit_rate(self) -> float:
        """Angular-only mounts: arcsec per second of 'equivalent duration' (see
        `mount_align_sizing.angular_unit_rate`)."""
        return angular_unit_rate(self._settings)

    def _min_angular_arcsec(self) -> float:
        """The smallest angular move the mount accepts (see `mount_align_sizing`)."""
        return min_angular_arcsec(self._mount)

    def _sizing_distance_for(self, mode: TargetMode) -> float | None:
        if self._calibration_distance_m is not None:
            return self._calibration_distance_m(mode)
        return 10_000.0 if mode == "terrestrial" else None

    # ---- S6.0: manual moves on a mount without timed pulses ------------------
    def _nudge_cap_arcsec(self) -> float:
        return nudge_cap_arcsec(self._settings)

    def _uncalibrated_nudge_arcsec(self, camera_key: str) -> float:
        """AGENTS.md / #31 Phase A -- see `mount_align_positioning.uncalibrated_nudge_arcsec`."""
        return uncalibrated_nudge_arcsec(
            self._settings,
            camera_key,
            self._camera_geometry,
            lambda: self._sizing_distance_for(self._target_mode()),
        )

    def _cap_text(self, angular: bool) -> str:
        return cap_text(self._settings, angular)

    def _exclude_camera_bounded(
        self, key: str, failure: MeasurementFailureClass, reason: str
    ) -> None:
        self._calibration_failed_cameras.add(key)
        self._last_failure_classes[key] = failure
        self._sizing.bounded[key] = bounded_text(
            failure,
            max_pulse_ms=self._settings.max_calibration_pulse_ms,
            rate_preset=self._sizing.rate_preset,
        )
        self._sizing.log.append({"event": "bounded", "camera": key, "reason": reason})

    def _finish_calibration(self) -> None:
        """Show the run's verdict (`CalibrationResults.verdict`: a matrix per usable camera,
        degenerate/no-motion cameras explained -- real report 0270868c)."""
        verdict = self._results.verdict(self._sizing.bounded)
        self._calibration = verdict.matrices
        self._last_failure_classes.update(verdict.failure_classes)
        # None (no error banner) as long as *some* camera ended up with a
        # usable matrix -- a fully independent per-camera outcome now,
        # not an all-or-nothing one.
        self._last_error = None if self._calibration else "no usable calibration for either camera"
        self._result_label.setText("\n".join(verdict.lines))
        self._resume_auto_exposure()

    # S6.6a: the run's results live in `self._results`; these names stay readable for
    # diagnostics and the existing tests.
    @property
    def _calibration_partial(self) -> dict[str, dict[MountAxis, AxisResponse]]:
        return self._results.partial

    @property
    def _calibration_failed_cameras(self) -> set[str]:
        return self._results.failed_cameras

    def calibration_for(self, camera_key: str) -> CalibrationMatrix | None:
        """The camera's full 4-direction calibration matrix ("left"=Main,
        "right"=Guide), or None if none exists -- issue #39: guide-assisted
        reacquisition needs Guide's, and must fail explicitly without it."""
        return self._calibration.get(camera_key)

    def _on_nudge_clicked(self, camera_key: str, axis: MountAxis, direction: AxisDirection) -> None:
        try:
            self._begin_nudge(camera_key, axis, direction)
        except Exception as exc:
            self._fail_safe(f"could not start the move: {exc}", exc)
            raise

    def _begin_nudge(self, camera_key: str, axis: MountAxis, direction: AxisDirection) -> None:
        # Sized completely before anything is sent -- `mount_align_positioning.plan_nudge`
        # (uncalibrated: the fixed calibration step size; calibrated: the clicked direction's
        # own measured rate, half a frame, clamped to the safety cap; S6.0 angular-only).
        frame_getter = self._get_left_frame if camera_key == "left" else self._get_right_frame
        plan = plan_nudge(
            settings=self._settings,
            camera_key=camera_key,
            axis=axis,
            direction=direction,
            matrix=self._calibration.get(camera_key),
            peek_frame=frame_getter,
            mode=self._target_mode(),
            angular_only=self._mount_is_angular_only,
            uncalibrated_arcsec=lambda: self._uncalibrated_nudge_arcsec(camera_key),
            floor_arcsec=self._min_angular_arcsec,
        )
        if plan.error is not None:
            self._last_error = plan.error
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        if plan.move is None:
            self._result_label.setText(plan.notice or "")
            return
        move = plan.move
        mode = self._target_mode()
        self._last_failure_classes = {}
        # Paused across the whole before-pulse-after bracket, resumed
        # immediately after the "after" capture in _finish_nudge (or on
        # any early-exit path below) -- see the constructor's own
        # docstring and real incident ca728d27.
        self._pause_auto_exposure()
        # Issue #43: DISPLAY frame != MEASUREMENT frame. The nudge's BEFORE is either (a) the
        # last stability-VERIFIED at-rest reference, provably still valid because the runner
        # has accepted no sequence since (so repeated nudges stay instant), or (b) a fresh
        # verified capture (a single wait after any other movement / on the first nudge).
        # Never the latest displayed frame.
        reference = self._reusable_reference(mode)
        if reference is not None:
            self._after_nudge_before_capture(move, mode, reference)
            return
        self._request_capture(
            mode,
            label="nudge_before",
            after_monotonic=self._last_pulse_completed_at,
            then=lambda before: self._after_nudge_before_capture(move, mode, before),
        )

    def _after_nudge_before_capture(
        self, move: NudgeMove, mode: TargetMode, before: dict[str, Measurement] | None
    ) -> None:
        if not before:
            self._resume_auto_exposure()
            self._last_error = self._capture_failure_message(
                f"{self._missing_label(mode)} before pulsing"
            )
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        started = submit_axis_move(
            self._runner, self._mount_park, self._mount,
            move.axis, move.direction, move.duration_ms, move.arcsec,
            rate_preset=self._settings.rate_preset, settle_ms=self._settings.settle_ms,
        )
        if not started:
            self._resume_auto_exposure()
            return  # a move is already running
        self._pending = PendingAction(
            kind="nudge",
            before=before,
            mode=mode,
            duration_ms=move.duration_ms,
            clamped=move.clamped,
            axis=move.axis,
            direction=move.direction,
        )
        suffix = (
            f" (safety-capped at {self._cap_text(move.arcsec is not None)} -- "
            "click again to continue)"
            if move.clamped
            else ""
        )
        axis_label = AXIS_LABELS[move.axis]
        direction_label = "+" if move.direction is AxisDirection.POSITIVE else "-"
        self._result_label.setText(
            f"Moving {CAMERA_LABELS[move.camera_key]} {axis_label} {direction_label}…{suffix}"
        )
        self._update_buttons_enabled()

    def _finish_nudge(
        self,
        pending: PendingAction,
        *,
        pulsed: bool,
        pulse_error: str | None,
        completed_at: float,
    ) -> None:
        if not pulsed:
            self._resume_auto_exposure()
            self._last_error = pulse_error or "pulse failed"
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        # See _last_pulse_completed_at's own docstring.
        self._last_pulse_completed_at = completed_at
        assert pending.axis is not None and pending.direction is not None
        self._last_movement_context = CommandedMovementContext(
            movement_type=pending.kind,
            duration_ms=pending.duration_ms,
            rate_preset=self._settings.rate_preset,
            axis=pending.axis.name,
            direction=pending.direction.name,
            minimum_settle_ms=self._settings.frame_settle_ms,
        )
        self._request_capture(
            pending.mode,
            label="nudge_after",
            after_monotonic=completed_at,
            then=lambda after: self._after_nudge_after_capture(pending, after),
        )

    def _after_nudge_after_capture(
        self, pending: PendingAction, after: dict[str, Measurement] | None
    ) -> None:
        self._resume_auto_exposure()
        assert pending.axis is not None and pending.direction is not None
        axis, direction = pending.axis, pending.direction
        if not after:
            self._last_error = self._capture_failure_message(
                f"{self._missing_label(pending.mode)} after the move"
            )
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        # Per-camera confirmation -- `mount_align_positioning.evaluate_nudge`: the move itself
        # already happened; an unconfirmed camera never withholds another camera's reading.
        outcome = evaluate_nudge(
            pending.before,
            after,
            lambda before, after_: self._build_response(
                pending.mode, axis, direction, pending.duration_ms, before, after_
            ),
            clamped=pending.clamped,
            cap_text=lambda: self._cap_text(self._mount_is_angular_only()),
        )
        self._last_failure_classes.update(outcome.failure_classes)
        self._last_responses = outcome.responses or self._last_responses
        self._last_error = outcome.error
        self._result_label.setText(outcome.text)

    def _screen_move_size(self) -> MovementSize:
        for size, button in self._size_buttons.items():
            if button.isChecked():
                return size
        return MovementSize.SMALL  # unreachable -- the group always has one checked

    def _screen_move_fraction(self) -> float:
        return {
            MovementSize.SMALL: self._settings.screen_move_small_fraction,
            MovementSize.MEDIUM: self._settings.screen_move_medium_fraction,
            MovementSize.LARGE: self._settings.screen_move_large_fraction,
        }[self._screen_move_size()]

    def _on_screen_move_clicked(self, camera_key: str, screen_direction: ScreenDirection) -> None:
        """Issue #31 Phase D: solves and submits a (possibly two-axis)
        pulse sequence that moves this camera's own image the requested
        on-screen direction, by the currently-selected frame-relative
        size (`mount_align_positioning.plan_screen_move`). Deliberately never
        captures/measures an "after" frame -- see module docstring's own
        Phase E paragraph for why a positioning click stays fire-and-forget
        rather than blocking on a stability-verified measurement the way a
        calibration step or a RA/Dec nudge does."""
        matrix = self._calibration.get(camera_key)
        if matrix is None:
            return  # shouldn't be reachable -- the button stays disabled without one
        frame_getter = self._get_left_frame if camera_key == "left" else self._get_right_frame
        peek_frame = frame_getter()
        plan = plan_screen_move(
            settings=self._settings,
            camera_key=camera_key,
            matrix=matrix,
            screen_direction=screen_direction,
            fraction=self._screen_move_fraction() if peek_frame is not None else 0.0,
            frame=peek_frame,
            mode=self._target_mode(),
            angular_only=self._mount_is_angular_only,
            floor_arcsec=self._min_angular_arcsec,
        )
        if plan.error is not None:
            self._last_error = plan.error
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        if plan.motion is None:
            self._result_label.setText(plan.notice or "")
            return
        angular = plan.angular
        skipped_note = (
            f" (skipped a {plan.skipped} component below the mount's smallest move)"
            if plan.skipped
            else ""
        )
        started = submit_motion_sequence(
            self._runner, self._mount_park, self._mount, plan.motion,
            rate_preset=self._settings.rate_preset, settle_ms=self._settings.settle_ms,
        )
        if not started:
            return  # a move is already running
        clamped = plan.clamped
        size_label = self._screen_move_size().value.capitalize()
        label = f"{CAMERA_LABELS[camera_key]} {screen_direction.name.lower()} ({size_label})"
        self._pending = PendingAction(
            kind="screen_move", before={}, mode=self._target_mode(), label=label, clamped=clamped,
        )
        suffix = f" (safety-capped at {self._cap_text(angular)}/step)" if clamped else ""
        self._result_label.setText(f"Moving {label}…{suffix}{skipped_note}")
        self._update_buttons_enabled()

    def _finish_screen_move(
        self,
        pending: PendingAction,
        *,
        pulsed: bool,
        pulse_error: str | None,
        completed_at: float,
    ) -> None:
        """Issue #31 Phase E: no "after" capture here -- see
        `_on_screen_move_clicked`'s own docstring. The pulse itself
        already ran regardless of anything below; there is no
        measurement to confirm or fail."""
        if not pulsed:
            self._last_error = pulse_error or "pulse failed"
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        self._last_pulse_completed_at = completed_at
        self._last_error = None  # a completed move must not keep an earlier move's error
        suffix = (
            f" (safety-capped at {self._cap_text(self._mount_is_angular_only())}/step -- "
            "click again to continue toward the target)"
            if pending.clamped
            else ""
        )
        self._result_label.setText(f"{pending.label} — done.{suffix}")

    def _on_stop(self) -> None:
        # S6.0c: every call here returns at once -- the runner sends no further step, and the
        # mount adapter's abort() hands OnStep's stop to a worker (it never queues behind the
        # GOTO it stops). Duck-typed -- see module docstring's "Stop" section for why this
        # isn't a MountPort Protocol method.
        self._runner.abort()
        abort = getattr(self._mount, "abort", None)
        if callable(abort):
            abort()
        # Issue #49: Stop ends the whole calibration/nudge, not just the current pulse: nothing
        # queued or in flight may carry on afterwards, and the panel is retryable at once.
        in_flight = self._capture_job is not None
        if self._calibration_queue or self._pending is not None or in_flight:
            if self._sizing.angular_only:  # S6.0: angular moves -- report the net sky angle
                net: dict[str, object] = {
                    a.name: round(arcsec)
                    for a, arcsec in self._sizing.axis_net_arcsec.items()
                    if arcsec
                }
                unit = "arcsec"
            else:
                net = {a.name: ms for a, ms in self._sizing.axis_net_pulse_ms.items() if ms}
                unit = "ms"
            self._cancel_activity()
            note = (
                f" (mount may be off its start position: net commanded {unit} {net})" if net else ""
            )
            self._last_error = "stopped by the user"
            if self._runner.is_busy:
                # The interrupted adapter call is still ending (OnStepAdapter has no cancel
                # hook; it ends at its own timeout); the poll loop re-enables the controls then.
                self._result_label.setText(
                    f"Stopped by the user{note} -- stop sent to the mount; the controls return "
                    "once the interrupted move call has ended."
                )
            else:
                self._result_label.setText(f"Stopped by the user{note}.")
            self._update_buttons_enabled()

    def _poll(self) -> None:
        if not self._connected:
            return
        try:
            self._poll_inner()
        except Exception as exc:
            # Issue #49: an unexpected exception in ANY continuation must not strand the panel
            # busy; reset first, then re-raise so the app's excepthook still records an incident.
            self._fail_safe(f"internal error: {exc}", exc)
            raise

    def _poll_inner(self) -> None:
        job = self._capture_job
        if job is not None:
            self._poll_capture_job(job)
            self._update_buttons_enabled()
            return
        outcome = self._runner.take_latest()
        if outcome is not None:
            # The reference point for "frames in movement are picked" --
            # see _capture_both's own `after_monotonic` docstring. Taken
            # right as the runner reports done (pulse+settle physically
            # complete), not later, so any Qt-event-loop delay before the
            # "after" capture actually runs only ever makes the freshness
            # check stricter, never looser.
            completed_at = self._clock.monotonic()
            # S6.7 review: EVERY runner outcome may have moved the mount -- also one whose
            # pending action Stop dropped, and a failed calibration's stranded return -- so its
            # end is always the next BEFORE capture's motion reference (frames exposed during
            # that move must never pass as fresh).
            self._last_pulse_completed_at = completed_at
            self._last_motion_paths = outcome.motion_paths
            self._last_pulse_timing = {
                "motion_started_at": outcome.motion_started_at,
                "motion_ended_at": outcome.motion_ended_at,
                "settled_at": outcome.settled_at,
                "completed_at": completed_at,
            }
            pending = self._pending
            self._pending = None
            if pending is not None:
                if pending.kind == "calibration":
                    self._finish_calibration_step(
                        pending, pulsed=outcome.pulsed, pulse_error=outcome.error,
                        completed_at=completed_at,
                    )
                elif pending.kind == "nudge":
                    self._finish_nudge(
                        pending, pulsed=outcome.pulsed, pulse_error=outcome.error,
                        completed_at=completed_at,
                    )
                else:
                    self._finish_screen_move(
                        pending, pulsed=outcome.pulsed, pulse_error=outcome.error,
                        completed_at=completed_at,
                    )
            elif self._awaiting_stranded_return:
                # The fire-and-forget return pulse _abort_calibration()
                # submitted has now actually finished -- see that
                # method's own docstring for why the "failed" message
                # shown when it was submitted said "returning..." rather
                # than reading as final.
                self._awaiting_stranded_return = False
                if outcome.pulsed:
                    suffix = "mount returned to start position"
                else:
                    suffix = (
                        "WARNING: mount may not have returned to start position -- "
                        f"{outcome.error}"
                    )
                self._result_label.setText(f"Calibration failed: {self._last_error} ({suffix})")
        self._update_buttons_enabled()

    def _build_response(
        self,
        mode: TargetMode,
        axis: MountAxis,
        direction: AxisDirection,
        pulse_ms: int,
        before: Measurement,
        after: Measurement,
    ) -> AxisResponse | None:
        """One camera's `AxisResponse` (see `mount_align_measurement.build_response`)."""
        return build_response(mode, axis, direction, pulse_ms, before, after)

    def interface_state(self) -> MountInterfaceState:
        """Issue #42: derived fresh from the live adapters every call (see
        `mount_align_interface.mount_interface_state`)."""
        return mount_interface_state(
            connected=self._connected, mount_park=self._mount_park, mount=self._mount
        )

    def _update_buttons_enabled(self) -> None:
        interface = self.interface_state()
        busy = self._runner.is_busy or self._capture_job is not None
        ready = interface.available and not busy
        self._run_calibration_button.setEnabled(ready)
        self._stop_button.setEnabled(self._connected and busy)
        # Real request: RA+/RA-/Dec+/Dec- must be usable to move the mount
        # by RA/Dec directly, not held hostage to a calibration matrix
        # existing at all -- real diagnostic 5e71b958 ("Calibration failed
        # again and move buttons not accessible"): terrestrial calibration
        # can genuinely fail for reasons that have nothing to do with
        # whether the mount itself can still be moved (e.g. a camera's own
        # view lacking enough structure to correlate, or one axis reading
        # a real physical zero this run -- neither stops a raw axis pulse
        # from working). Gated the same as Run Calibration itself
        # (`ready` alone) -- see `_on_nudge_clicked`'s own docstring for
        # how it now solves a duration with or without a real
        # per-camera calibration.
        for buttons in self._nudge_buttons.values():
            for button in buttons.values():
                button.setEnabled(ready)

        # Issue #31 Phase D: unlike the RA/Dec pad above, the
        # screen-relative pad needs a real, non-degenerate 4-direction
        # response model for *this specific camera* to solve from --
        # enabled only once `self._calibration` actually has one (set in
        # `_finish_calibration`), same "camera-independent" spirit as
        # the RA/Dec pad's own docstring, just with a real prerequisite
        # this time instead of none.
        for camera_key, screen_buttons in self._screen_move_buttons.items():
            for button in screen_buttons.values():
                button.setEnabled(ready and camera_key in self._calibration)

        # Explain *why* the calibration/nudge buttons are disabled, rather
        # than leaving them silently unresponsive -- see module docstring's
        # incident note. Never stomps a "Calibrating…"/"Moving…"/result/
        # error message that's still relevant.
        self._sync_availability_message(interface, busy=busy)

    def _sync_availability_message(self, interface: MountInterfaceState, *, busy: bool) -> None:
        """The status text is owned by `interface_state()`: shown while the mount
        interface is unavailable, CLEARED as soon as it is available (or Mount Align
        disconnects) -- and only ever replaces/clears its own previous message, never a
        "Calibrating...", result or error text."""
        owned = self._availability_message
        if interface.available or interface.code == "not_connected":
            if owned is not None and self._result_label.text() == owned:
                self._result_label.setText("")
            self._availability_message = None
            return
        if busy:
            return
        message = f"Mount interface not available: {interface.detail}."
        current = self._result_label.text()
        if current != message and (owned is None or current == owned or not current):
            self._result_label.setText(message)
            self._availability_message = message

    def diagnostic_capture_timeline(self) -> list[dict[str, Any]]:
        """Issue #43: the bounded per-capture timeline -- reference / pulse timing, each frame's
        captured_at / exposure / computed exposure start, and the stability score/tier -- for
        proving overlap or non-overlap after the fact."""
        return [dict(entry) for entry in self._capture_timeline]

    def diagnostic_context(self) -> dict[str, Any]:
        # Issue #48: the global mode and the measurement it selects -- the collimation TARGET
        # type (natural/artificial star) is reported separately by the main window.
        context: dict[str, Any] = {
            "operating_mode": self._operating_mode().value,
            "measurement_mode": self._target_mode(),
        }
        context["activity"] = self.diagnostic_activity()
        interface = self.interface_state()
        context["mount_interface"] = {
            "available": interface.available,
            "code": interface.code,
            "detail": interface.detail,
            "connected": self._connected,
            "park_available": self._mount_park.status().available,
            "pulse_connected": self._mount.status().connected,
            "park_adapter": type(self._mount_park).__name__,
            "pulse_adapter": type(self._mount).__name__,
            "last_connect_error": self._last_connect_error,
        }
        if self._sizing.log:
            context["calibration_sizing"] = list(self._sizing.log)
        # Deliberately sourced from _calibration_partial (every axis
        # response actually measured), not self._calibration (only the
        # non-degenerate matrices -- see is_degenerate() and
        # _finish_calibration()'s own docstring). A degenerate run is
        # exactly the case a diagnostic bundle most needs the raw numbers
        # for: diagnostic 0270868c's own root cause (AXIS1 measured a
        # confident (0, 0) on both cameras) was only readable from these
        # exact axis1/axis2 values.
        context["calibration"] = {
            key: {
                "axis1": response_dict(responses[MountAxis.AXIS1]),
                "axis2": response_dict(responses[MountAxis.AXIS2]),
            }
            for key, responses in self._calibration_partial.items()
            if MountAxis.AXIS1 in responses and MountAxis.AXIS2 in responses
        }
        if self._last_error is not None:
            context["last_result"] = {"error": self._last_error}
        elif self._last_responses is None:
            context["last_result"] = None
        else:
            context["last_result"] = {
                key: response_dict(response) for key, response in self._last_responses.items()
            }
        # Issue #27: which of the three independent layers (capture,
        # match, calibration-derivation) a failure actually belongs to,
        # per camera -- distinct from last_error's free-text message, so
        # a pulled bundle can group/filter failures programmatically
        # instead of parsing English. Empty when nothing failed this
        # attempt.
        context["last_failure_classes"] = {
            key: failure_class.value for key, failure_class in self._last_failure_classes.items()
        }
        return context

    def diagnostic_backlash_evidence(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Issue #31 Phase C's own UUID-diagnostics ask: each camera's
        per-(axis, direction) backlash characterization from the most
        recent Run Calibration attempt -- e.g. `{"left": {"axis1_positive":
        {"first": {...}, "repeat": {...}, "backlash_deficit_px": ...}}}`.
        Folded into `MainWindow`'s diagnostic bundle alongside
        `diagnostic_stability_evidence()` -- same "what would have let a
        pulled bundle answer this question" reasoning that method's own
        docstring gives for stability, applied here to backlash instead."""
        return {
            camera_key: {
                f"{axis.name.lower()}_{direction.name.lower()}": {
                    "first": response_dict(characterization.first),
                    "repeat": response_dict(characterization.repeat),
                    "backlash_deficit_px": characterization.backlash_deficit_px,
                }
                for (axis, direction), characterization in characterizations.items()
            }
            for camera_key, characterizations in self._results.characterizations.items()
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected."""
        self._timer.stop()
        job = self._capture_job
        if job is not None:
            job.cancel.set()
            self._capture_job = None
        if self._connected:
            self._mount.disconnect()
            self._connected = False
        # Defensive -- if the window closes mid-calibration/nudge, don't
        # leave the (independently-lived) CameraPanels' auto-exposure
        # paused forever for whatever streaming happens after this panel
        # is gone. A no-op if nothing was paused.
        self._resume_auto_exposure()








