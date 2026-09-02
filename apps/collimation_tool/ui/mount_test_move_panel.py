"""MountTestMovePanel — mount-alignment tool: calibrate how each mount axis
moves *each camera's own frame*, then offer per-camera Up/Down/Left/Right
buttons that pulse the mount in whatever combination actually produces that
on-screen direction for that specific camera.

Reworked from the original "Test Move" diagnostic (raw N/S/E/W buttons that
just reported the resulting displacement as text) into an alignment tool
requested directly: "aligning primary and secondary scope" needs direction
buttons that are correct *per camera*, since Main and Guide can be rotated
differently relative to each other and to the mount's RA/Dec axes — reading
dx/dy/angle numbers after each raw-axis click and inferring the mapping by
hand doesn't scale to that. See `astrotool_core.mount.axis_calibration`'s
`compose_screen_move` for the actual two-axis inversion this is built on.

Own `MountPort` connection, separate from `MountParkPanel`'s `MountParkPort`
connection to the same device (same pattern as
`IndiFocuserAdapter`/`IndiMountParkAdapter` already being two independent
`IndiClient` sockets to one INDI device) — see
`astrotool_core.mount.indi_mount_pulse_adapter`'s docstring for the real
INDI properties this drives (`TELESCOPE_SLEW_RATE`, `TELESCOPE_MOTION_NS`/
`_WE`).

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
hardware check found OnStep's driver refuses `TELESCOPE_MOTION_NS`/`_WE`
while parked — a deliberate safety interlock, not a defect) and then never
re-parks (`park_after=False` on every call here) — "Run Calibration" and the
direction pads are meant to run one after another across a single unparked
working session, and re-parking after each pulse would undo the point of
staying unparked between them. Parking back up when done stays the separate
Mount panel's job, same as it already is for every other unparked action in
this app.

"Run Calibration" runs a fixed four-pulse sequence (see `_CALIBRATION_STEPS`):
pulse AXIS1 positive, measure the resulting displacement in both cameras,
pulse AXIS1 negative to return (trusted symmetric — no re-measurement, same
duration/rate as the forward pulse), then the same for AXIS2. Each camera's
two measured `AxisResponse`s become a `CalibrationMatrix`
(`astrotool_core.mount.axis_calibration`) once both axes are measured, at
which point that camera's four direction-pad buttons enable. Aborts the
whole sequence (clearing any partial result) the moment any step fails to
pulse or measure — a half-built calibration is worse than none, since a
direction button would then be silently wrong for whichever axis never got
re-measured.

Clicking a direction-pad button solves `compose_screen_move` for that
camera's own calibration and the clicked direction, submits the resulting
1-2 pulses back-to-back via `MountTestMoveRunner.submit_sequence`, and
reports the resulting displacement the same way a calibration step does —
reusing `_capture`/`_build_response`/`_format_response` unchanged. A
degenerate calibration (AXIS1/AXIS2 responses too close to parallel to
invert) surfaces as an error asking the user to recalibrate rather than
sending a wild pulse.

Real hardware motion with no way to interrupt it once started is a real
safety gap (incident 9551627f) — the "Stop" button, wired to
`IndiMountPulseAdapter.abort()` (`TELESCOPE_ABORT_MOTION`) via duck-typing
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
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount.axis_calibration import (
    AxisResponse,
    CalibrationMatrix,
    compose_screen_move,
    is_degenerate,
    response_from_positions,
)
from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.mount.port import AxisDirection, MountAxis, MountPort
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.translation_offset import measure_translation_offset
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner

FrameGetter = Callable[[], np.ndarray | None]
#: (reference_monotonic, timeout_s) -> a frame captured entirely after
#: reference_monotonic, or None on timeout -- see CameraPanel's own
#: wait_for_frame_after() docstring (real report: "frames in movement
#: are picked on the calibration, causing guide to fail").
FreshFrameWaiter = Callable[[float, float], "np.ndarray | None"]
#: How long an "after" capture waits for a frame that's provably fresh
#: (its own exposure entirely postdates the pulse+settle completion)
#: before giving up -- treated the same as any other missing "after"
#: frame (see _finish_calibration_step/_finish_nudge's own "no frame
#: available" abort path), not silently downgraded to a stale one.
_FRESH_FRAME_TIMEOUT_S = 2.0

#: "star" measures a point-source centroid via detect_sources() (precise,
#: but needs an actual star -- see incident 6fa2aa59: correctly refuses
#: otherwise). "terrestrial" instead cross-correlates the whole before/
#: after frame via measure_translation_offset() -- works on any textured
#: scene (indoors, daytime), whole-pixel precision only. See
#: MountTestMovePanel's own "Target" toggle.
TargetMode = Literal["star", "terrestrial"]

#: What a captured "before"/"after" measurement looks like in each mode --
#: a star's (x, y) centroid, or the whole mono frame to cross-correlate
#: against later.
_Measurement = tuple[float, float] | np.ndarray

_POLL_INTERVAL_MS = 250

_CAMERA_LABELS = {"left": "Main", "right": "Guide"}
_AXIS_LABELS = {MountAxis.AXIS1: "RA-axis", MountAxis.AXIS2: "Dec-axis"}
_OTHER_CAMERA = {"left": "right", "right": "left"}

#: Right/Left are the horizontal frame axis, Down/Up the vertical one, in
#: the same x-right/y-down image-space convention as `AxisResponse.angle_degrees`.
_SCREEN_DIRECTIONS: dict[str, tuple[float, float]] = {
    "Up": (0.0, -1.0),
    "Down": (0.0, 1.0),
    "Left": (-1.0, 0.0),
    "Right": (1.0, 0.0),
}


@dataclass(frozen=True)
class _CalibrationStep:
    """One pulse within the fixed calibration sequence. `measure=False`
    marks a "move back" return pulse -- same axis/duration/rate, opposite
    direction, no frame capture (a symmetric response is trusted, not
    re-verified -- see module docstring)."""

    axis: MountAxis
    direction: AxisDirection
    measure: bool


#: AXIS1 positive, measure; AXIS1 negative, return; AXIS2 positive, measure;
#: AXIS2 negative, return. Order doesn't matter functionally -- AXIS1 first
#: is arbitrary.
_CALIBRATION_STEPS: tuple[_CalibrationStep, ...] = (
    _CalibrationStep(MountAxis.AXIS1, AxisDirection.POSITIVE, measure=True),
    _CalibrationStep(MountAxis.AXIS1, AxisDirection.NEGATIVE, measure=False),
    _CalibrationStep(MountAxis.AXIS2, AxisDirection.POSITIVE, measure=True),
    _CalibrationStep(MountAxis.AXIS2, AxisDirection.NEGATIVE, measure=False),
)


@dataclass
class _PendingAction:
    """State carried from a submit call to the matching `_poll()` completion
    -- generalizes the old single set of `_pending_*` fields to cover both
    a calibration step and a direction-pad nudge."""

    kind: Literal["calibration", "nudge"]
    before: dict[str, _Measurement]
    mode: TargetMode
    step: _CalibrationStep | None = None
    #: Total pulse duration across every sub-pulse in a nudge's composed
    #: move (unused for a calibration step, which has its own step.axis to
    #: report against instead).
    duration_ms: int = 0


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
        wait_for_left_frame: FreshFrameWaiter | None = None,
        wait_for_right_frame: FreshFrameWaiter | None = None,
    ) -> None:
        super().__init__()
        self._mount = mount
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
        self._wait_for_left_frame: FreshFrameWaiter = wait_for_left_frame or (
            lambda _reference, _timeout: self._get_left_frame()
        )
        self._wait_for_right_frame: FreshFrameWaiter = wait_for_right_frame or (
            lambda _reference, _timeout: self._get_right_frame()
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
        self._pending: _PendingAction | None = None
        self._calibration_queue: list[_CalibrationStep] = []
        self._calibration_partial: dict[str, dict[MountAxis, AxisResponse]] = {
            "left": {}, "right": {},
        }
        self._calibration: dict[str, CalibrationMatrix] = {}
        self._last_responses: dict[str, AxisResponse] | None = None
        self._last_error: str | None = None
        #: True between _abort_calibration() submitting a stranded return
        #: pulse and that pulse actually finishing -- see that method's
        #: own docstring and _poll()'s use of this flag.
        self._awaiting_stranded_return = False
        #: Cameras excluded from the *current* Run Calibration attempt --
        #: see _finish_calibration_step()'s own docstring for why a
        #: camera's own "not enough structure" no longer aborts the whole
        #: sequence.
        self._calibration_failed_cameras: set[str] = set()
        #: See diagnostic_frames()'s own docstring.
        self._last_diagnostic_frames: dict[str, np.ndarray] = {}
        #: See diagnostic_camera_state()'s own docstring.
        self._last_diagnostic_camera_state: dict[str, tuple[float, int]] = {}

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")

        self._target_group = QButtonGroup(self)
        self._target_group.setExclusive(True)
        self._star_button = QPushButton("Star")
        self._star_button.setCheckable(True)
        self._star_button.setChecked(True)  # default -- unchanged prior behavior
        self._target_group.addButton(self._star_button)
        self._terrestrial_button = QPushButton("Terrestrial")
        self._terrestrial_button.setCheckable(True)
        self._target_group.addButton(self._terrestrial_button)
        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Target"))
        target_row.addWidget(self._star_button)
        target_row.addWidget(self._terrestrial_button)
        target_row.addStretch(1)

        self._run_calibration_button = QPushButton("Run Calibration")
        self._run_calibration_button.clicked.connect(self._on_run_calibration_clicked)
        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self._on_stop)
        calibration_row = QHBoxLayout()
        calibration_row.addWidget(
            QLabel(
                f"Calibration ({self._settings.pulse_ms}ms, "
                f"rate preset {self._settings.rate_preset})"
            )
        )
        calibration_row.addWidget(self._run_calibration_button)
        calibration_row.addWidget(self._stop_button)
        calibration_row.addStretch(1)

        self._nudge_buttons: dict[str, dict[str, QPushButton]] = {}
        main_pad_row = self._build_direction_pad("left")
        guide_pad_row = self._build_direction_pad("right")

        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(target_row)
        layout.addLayout(calibration_row)
        layout.addLayout(main_pad_row)
        layout.addLayout(guide_pad_row)
        layout.addWidget(self._result_label)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

        self._update_buttons_enabled()

    def _build_direction_pad(self, camera_key: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(f"{_CAMERA_LABELS[camera_key]}:"))
        buttons: dict[str, QPushButton] = {}
        for direction_name in ("Up", "Down", "Left", "Right"):
            button = QPushButton(direction_name)
            button.setEnabled(False)
            button.clicked.connect(
                lambda _checked=False, key=camera_key, name=direction_name: (
                    self._on_nudge_clicked(key, name)
                )
            )
            buttons[direction_name] = button
            row.addWidget(button)
        row.addStretch(1)
        self._nudge_buttons[camera_key] = buttons
        return row

    def _on_toggle_connect(self, checked: bool) -> None:
        if checked:
            try:
                self._mount.connect()
            except ConnectionError as exc:
                self._status_label.setText(f"Connect failed — {exc}")
                self._connect_button.blockSignals(True)
                self._connect_button.setChecked(False)
                self._connect_button.blockSignals(False)
                self._update_buttons_enabled()
                return
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

    def _target_mode(self) -> TargetMode:
        return "terrestrial" if self._terrestrial_button.isChecked() else "star"

    def _capture(self, mode: TargetMode, frame: np.ndarray | None) -> _Measurement | None:
        """One camera's "before"/"after" measurement in the given mode --
        a star centroid, or the whole frame itself (to cross-correlate
        against its counterpart later)."""
        if mode == "star":
            return _measure_brightest_source(frame)
        return frame  # "terrestrial" -- any frame at all is usable here

    def _missing_label(self, mode: TargetMode) -> str:
        return "no star detected" if mode == "star" else "no frame available"

    def _capture_both(
        self,
        mode: TargetMode,
        *,
        diagnostic_label: str | None = None,
        after_monotonic: float | None = None,
    ) -> dict[str, _Measurement] | None:
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
        own docstring. Only the "after" capture of a calibration
        step/nudge should pass this; the "before" capture has nothing to
        wait out (the mount hasn't moved yet)."""
        if after_monotonic is None:
            left_frame = self._get_left_frame()
            right_frame = self._get_right_frame()
        else:
            left_frame = self._wait_for_left_frame(after_monotonic, _FRESH_FRAME_TIMEOUT_S)
            right_frame = self._wait_for_right_frame(after_monotonic, _FRESH_FRAME_TIMEOUT_S)
        if diagnostic_label is not None:
            if left_frame is not None:
                self._last_diagnostic_frames[f"{diagnostic_label}_left"] = left_frame
                left_state = self._get_left_exposure_gain()
                if left_state is not None:
                    self._last_diagnostic_camera_state[f"{diagnostic_label}_left"] = left_state
            if right_frame is not None:
                self._last_diagnostic_frames[f"{diagnostic_label}_right"] = right_frame
                right_state = self._get_right_exposure_gain()
                if right_state is not None:
                    self._last_diagnostic_camera_state[f"{diagnostic_label}_right"] = right_state
        raw = {
            "left": self._capture(mode, left_frame),
            "right": self._capture(mode, right_frame),
        }
        missing = [key for key, measurement in raw.items() if measurement is None]
        if missing:
            return None
        return raw  # type: ignore[return-value]

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

    def _pause_auto_exposure(self) -> None:
        self._set_left_auto_exposure_paused(True)
        self._set_right_auto_exposure_paused(True)

    def _resume_auto_exposure(self) -> None:
        self._set_left_auto_exposure_paused(False)
        self._set_right_auto_exposure_paused(False)

    def _on_run_calibration_clicked(self) -> None:
        self._calibration_queue = list(_CALIBRATION_STEPS)
        self._calibration_partial = {"left": {}, "right": {}}
        self._calibration_failed_cameras = set()
        # Paused for the whole 4-step sequence, not just per-step -- see
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
        before: dict[str, _Measurement] = {}
        if step.measure:
            label = f"{step.axis.name.lower()}_before"
            captured = self._capture_both(mode, diagnostic_label=label)
            if captured is None:
                self._abort_calibration(f"{self._missing_label(mode)} before pulsing")
                return
            before = captured
        started = self._runner.submit(
            self._mount_park,
            self._mount,
            step.axis,
            step.direction,
            self._settings.pulse_ms,
            rate_preset=self._settings.rate_preset,
            park_after=False,
            settle_ms=self._settings.settle_ms,
        )
        if not started:
            self._abort_calibration("mount busy — could not start calibration pulse")
            return
        self._calibration_queue.pop(0)
        self._pending = _PendingAction(kind="calibration", before=before, mode=mode, step=step)
        self._result_label.setText(
            f"Calibrating {step.axis.name} ({step.direction.name.lower()})…"
        )
        self._update_buttons_enabled()

    def _abort_calibration(self, message: str, *, strand_return_step: bool = False) -> None:
        """Stop the calibration sequence and report `message`.

        `strand_return_step`: real incident a082144a -- if the step that
        just failed was a "test" pulse whose forward move already
        succeeded (only the *measurement* failed, after the fact), the
        mount is now sitting off its original position with its own
        paired "move back" return step still next in the queue. Clearing
        the whole queue unconditionally (the old behavior) silently
        dropped that return step too, leaving the mount stranded --
        "not returning to start point" was a real, reproducible bug, not
        user error. When true, that one return step (still at the front
        of `_calibration_queue`, by construction of `_CALIBRATION_STEPS`
        -- always immediately follows its own axis's test step) is
        submitted before the queue is cleared, fire-and-forget: nothing
        further in this calibration attempt depends on its outcome, and
        `_poll()` already tolerates a completion with no matching
        `_pending` (this is the same shape as any other untracked pulse).

        Real report ("calibration failed is stated already while mount
        is moving"): this used to show the final "Calibration failed"
        text immediately, then submit the stranded return pulse right
        after -- so the mount kept visibly moving for several more
        seconds under a message that already read as final/settled.
        When a return step is actually submitted, the message now says
        so explicitly, and `_poll()` (via `_awaiting_stranded_return`)
        updates it once that return pulse actually finishes -- to a
        confirmation if it succeeded, or a explicit warning if it didn't
        (real pulses can still fail -- see MountTestMoveRunner's own
        retry logic, which already covers transient rejection but not
        every possible failure).
        """
        return_step = self._calibration_queue[0] if strand_return_step else None
        self._calibration_queue = []
        self._pending = None
        self._last_error = message
        suffix = " (returning mount to start position…)" if return_step is not None else ""
        self._result_label.setText(f"Calibration failed: {message}{suffix}")
        self._resume_auto_exposure()
        if return_step is not None:
            self._awaiting_stranded_return = True
            self._runner.submit(
                self._mount_park,
                self._mount,
                return_step.axis,
                return_step.direction,
                self._settings.pulse_ms,
                rate_preset=self._settings.rate_preset,
                park_after=False,
                settle_ms=self._settings.settle_ms,
            )
        self._update_buttons_enabled()

    def _finish_calibration_step(
        self,
        pending: _PendingAction,
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
        (still via `strand_return_step`, same as any other abort after a
        successful forward pulse).

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
            self._abort_calibration(pulse_error or "pulse failed")
            return
        if step.measure:
            after = self._capture_both(
                pending.mode,
                diagnostic_label=f"{step.axis.name.lower()}_after",
                after_monotonic=completed_at,
            )
            if after is None:
                self._abort_calibration(
                    f"{self._missing_label(pending.mode)} after the move",
                    strand_return_step=True,
                )
                return
            responses: dict[str, AxisResponse] = {}
            newly_failed: list[str] = []
            for key in ("left", "right"):
                if key in self._calibration_failed_cameras:
                    continue  # already excluded -- no point re-measuring it
                response = self._build_response(
                    pending.mode, step.axis, step.direction, self._settings.pulse_ms,
                    pending.before[key], after[key],
                )
                if response is None:
                    newly_failed.append(key)
                    self._calibration_failed_cameras.add(key)
                else:
                    responses[key] = response
                    self._calibration_partial[key][step.axis] = response
            if len(self._calibration_failed_cameras) >= 2:
                self._abort_calibration(
                    "not enough structure to measure a displacement in either camera",
                    strand_return_step=True,
                )
                return
            if newly_failed:
                self._last_error = (
                    f"not enough structure to measure a displacement in: "
                    f"{', '.join(newly_failed)} -- excluded from this calibration"
                )
            elif responses:
                self._last_error = None
            self._last_responses = responses or self._last_responses
        self._start_next_calibration_step()

    def _finish_calibration(self) -> None:
        """Build each camera's `CalibrationMatrix`, unless its own
        AXIS1/AXIS2 responses are degenerate -- real report (diagnostic
        0270868c): the driver can report a pulse fully accepted (both
        motion-on and motion-off confirmed) while producing no real,
        measurable mount motion (most likely a real, intermittent
        mount/cable issue given the pattern recurring across sessions on
        alternating axes -- not a rejected-pulse case, which
        `MountTestMoveRunner` already retries through separately). Storing
        that as a "successful" matrix anyway used to only surface the
        problem later, confusingly, the first time a nudge button called
        `compose_screen_move` and hit this exact same check for a
        different reason. Checking it here, right when calibration
        finishes, means the affected camera's nudge buttons simply never
        enable (same as the no-matrix state) and the user sees a specific,
        actionable message immediately instead."""
        self._calibration = {}
        lines: list[str] = []
        for key in ("left", "right"):
            if key in self._calibration_failed_cameras:
                # See _finish_calibration_step()'s own docstring (real
                # report d14c3a9b) -- this camera's own measurement
                # failed partway through, but that no longer took the
                # other camera's calibration down with it.
                lines.append(
                    f"{_CAMERA_LABELS[key]}: not enough structure to measure a displacement "
                    "-- excluded from this calibration (driver accepted every pulse; check "
                    "this camera's own exposure/gain and target)."
                )
                continue
            axis1 = self._calibration_partial[key].get(MountAxis.AXIS1)
            axis2 = self._calibration_partial[key].get(MountAxis.AXIS2)
            if axis1 is None or axis2 is None:
                continue  # shouldn't happen unless _abort_calibration already fired
            if is_degenerate(axis1, axis2):
                lines.append(
                    _degenerate_calibration_message(
                        key, axis1, axis2, self._calibration_partial[_OTHER_CAMERA[key]]
                    )
                )
                continue
            self._calibration[key] = CalibrationMatrix(
                responses={
                    (MountAxis.AXIS1, AxisDirection.POSITIVE): axis1,
                    (MountAxis.AXIS2, AxisDirection.POSITIVE): axis2,
                }
            )
            lines.append(
                f"{_CAMERA_LABELS[key]}: RA-axis {_format_response(axis1)} | "
                f"Dec-axis {_format_response(axis2)}"
            )
        # None (no error banner) as long as *some* camera ended up with a
        # usable matrix -- a fully independent per-camera outcome now,
        # not an all-or-nothing one.
        self._last_error = None if self._calibration else "no usable calibration for either camera"
        self._result_label.setText("\n".join(lines))
        self._resume_auto_exposure()

    def _on_nudge_clicked(self, camera_key: str, direction_name: str) -> None:
        matrix = self._calibration.get(camera_key)
        if matrix is None:
            return  # defensive -- button should be disabled without a matrix
        axis1_response = matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
        axis2_response = matrix.response_for(MountAxis.AXIS2, AxisDirection.POSITIVE)
        unit_dx, unit_dy = _SCREEN_DIRECTIONS[direction_name]
        # Real request: a nudge should move a large, decisive distance
        # for rough alignment -- half *this camera's own* frame width
        # (Left/Right) or height (Up/Down) -- not a small fixed pixel
        # count (a future "slow down near target" fine-adjustment mode
        # is explicitly deferred, not this). Main and Guide have very
        # different resolutions, so this needs the clicked camera's
        # actual frame dimensions, not a shared constant -- peek at one
        # frame before computing the target (cheap: the same cached read
        # the "before" capture below makes moments later anyway).
        frame_getter = self._get_left_frame if camera_key == "left" else self._get_right_frame
        peek_frame = frame_getter()
        if peek_frame is None:
            self._last_error = f"{self._missing_label(self._target_mode())} before pulsing"
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        frame_height, frame_width = peek_frame.shape[:2]
        target_dx_px = unit_dx * frame_width * self._settings.nudge_target_fraction
        target_dy_px = unit_dy * frame_height * self._settings.nudge_target_fraction
        try:
            steps = compose_screen_move(
                axis1_response, axis2_response, target_dx_px=target_dx_px, target_dy_px=target_dy_px
            )
        except ValueError as exc:
            self._last_error = str(exc)
            self._result_label.setText(f"Move failed: {exc} — try Run Calibration again.")
            return
        if not steps:
            self._result_label.setText(
                f"{_CAMERA_LABELS[camera_key]}: already aligned for {direction_name.lower()}."
            )
            return

        mode = self._target_mode()
        # Paused across the whole before-pulse-after bracket, resumed
        # immediately after the "after" capture in _finish_nudge (or on
        # any early-exit path below) -- see the constructor's own
        # docstring and real incident ca728d27.
        self._pause_auto_exposure()
        before = self._capture_both(mode, diagnostic_label="nudge_before")
        if before is None:
            self._resume_auto_exposure()
            self._last_error = f"{self._missing_label(mode)} before pulsing"
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        started = self._runner.submit_sequence(
            self._mount_park, self._mount, steps,
            rate_preset=self._settings.rate_preset, park_after=False,
            settle_ms=self._settings.settle_ms,
        )
        if not started:
            self._resume_auto_exposure()
            return  # a move is already running
        self._pending = _PendingAction(
            kind="nudge",
            before=before,
            mode=mode,
            duration_ms=sum(duration_ms for _, _, duration_ms in steps),
        )
        self._result_label.setText(
            f"Moving {_CAMERA_LABELS[camera_key]} {direction_name.lower()}…"
        )
        self._update_buttons_enabled()

    def _finish_nudge(
        self,
        pending: _PendingAction,
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
        after = self._capture_both(
            pending.mode, diagnostic_label="nudge_after", after_monotonic=completed_at
        )
        self._resume_auto_exposure()
        if after is None:
            self._last_error = f"{self._missing_label(pending.mode)} after the move"
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        responses: dict[str, AxisResponse] = {}
        failed: list[str] = []
        for key in ("left", "right"):
            # axis/direction are a display-only placeholder here (unused by
            # _format_response) -- a composed move blends both real axes,
            # it doesn't correspond to a single one. duration_ms is the
            # real total elapsed time across every sub-pulse.
            response = self._build_response(
                pending.mode, MountAxis.AXIS1, AxisDirection.POSITIVE, pending.duration_ms,
                pending.before[key], after[key],
            )
            if response is None:
                failed.append(key)
            else:
                responses[key] = response
        if failed:
            self._last_error = (
                f"not enough structure to measure a displacement in: {', '.join(failed)}"
            )
            self._result_label.setText(f"Move failed: {self._last_error}")
            return
        self._last_responses = responses
        self._last_error = None
        parts = [
            f"{_CAMERA_LABELS[key]}: {_format_response(response)}"
            for key, response in responses.items()
        ]
        self._result_label.setText(" | ".join(parts))

    def _on_stop(self) -> None:
        # Duck-typed -- see module docstring's "Stop" section for why
        # this isn't a MountPort Protocol method.
        abort = getattr(self._mount, "abort", None)
        if callable(abort):
            abort()

    def _poll(self) -> None:
        if not self._connected:
            return
        outcome = self._runner.take_latest()
        if outcome is not None:
            # The reference point for "frames in movement are picked" --
            # see _capture_both's own `after_monotonic` docstring. Taken
            # right as the runner reports done (pulse+settle physically
            # complete), not later, so any Qt-event-loop delay before the
            # "after" capture actually runs only ever makes the freshness
            # check stricter, never looser.
            completed_at = time.monotonic()
            pending = self._pending
            self._pending = None
            if pending is not None:
                if pending.kind == "calibration":
                    self._finish_calibration_step(
                        pending, pulsed=outcome.pulsed, pulse_error=outcome.error,
                        completed_at=completed_at,
                    )
                else:
                    self._finish_nudge(
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
        before: _Measurement,
        after: _Measurement,
    ) -> AxisResponse | None:
        """Build one camera's `AxisResponse` from its before/after
        measurement -- star mode already has two absolute centroid
        positions to hand `response_from_positions()` directly; terrestrial
        mode's whole-frame correlation already *is* the displacement, so
        it hands that in as the "after" position relative to a `(0, 0)`
        "before" instead. Returns None if terrestrial mode's correlation
        didn't find enough shared structure to trust (see
        `measure_translation_offset`'s own docstring)."""
        if mode == "star":
            assert isinstance(before, tuple) and isinstance(after, tuple)
            return response_from_positions(axis, direction, pulse_ms, before, after)
        assert isinstance(before, np.ndarray) and isinstance(after, np.ndarray)
        offset = measure_translation_offset(before, after)
        if offset is None:
            return None
        return response_from_positions(
            axis, direction, pulse_ms, (0.0, 0.0), (offset.dx_px, offset.dy_px)
        )

    def _update_buttons_enabled(self) -> None:
        park_status = self._mount_park.status()
        busy = self._runner.is_busy
        ready = self._connected and park_status.available and not busy
        self._run_calibration_button.setEnabled(ready)
        self._stop_button.setEnabled(self._connected and busy)
        for camera_key, buttons in self._nudge_buttons.items():
            has_matrix = camera_key in self._calibration
            for button in buttons.values():
                button.setEnabled(ready and has_matrix)

        # Explain *why* the calibration/nudge buttons are disabled, rather
        # than leaving them silently unresponsive -- see module docstring's
        # incident note. Never stomps a "Calibrating…"/"Moving…"/result/
        # error message that's still relevant.
        if busy or ready or not self._connected:
            return
        self._result_label.setText("Mount interface not available.")

    def diagnostic_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {"target_mode": self._target_mode()}
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
                "axis1": _response_dict(responses[MountAxis.AXIS1]),
                "axis2": _response_dict(responses[MountAxis.AXIS2]),
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
                key: _response_dict(response) for key, response in self._last_responses.items()
            }
        return context

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected."""
        self._timer.stop()
        if self._connected:
            self._mount.disconnect()
            self._connected = False
        # Defensive -- if the window closes mid-calibration/nudge, don't
        # leave the (independently-lived) CameraPanels' auto-exposure
        # paused forever for whatever streaming happens after this panel
        # is gone. A no-op if nothing was paused.
        self._resume_auto_exposure()


def _measure_brightest_source(frame: np.ndarray | None) -> tuple[float, float] | None:
    if frame is None:
        return None
    result = detect_sources(frame)
    if not result.sources:
        return None
    brightest = max(result.sources, key=lambda source: source.peak)
    return (brightest.x, brightest.y)


def _format_response(response: AxisResponse) -> str:
    return (
        f"dx={response.dx_px:+.1f}px dy={response.dy_px:+.1f}px "
        f"({response.magnitude_px:.1f}px @ {response.angle_degrees:.0f}°)"
    )


def _degenerate_calibration_message(
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
    other_label = _CAMERA_LABELS[_OTHER_CAMERA[camera_key]]
    notes: list[str] = []
    for axis in zero_axes:
        other = other_camera_responses.get(axis)
        if other is not None and other.magnitude_px > 0.0:
            notes.append(
                f"{_AXIS_LABELS[axis]} measured no motion here, but {other_label} confirms "
                "real motion on it -- likely this camera's own framing/plate scale, not a "
                "mount issue."
            )
        else:
            notes.append(
                f"{_AXIS_LABELS[axis]} measured no motion on either camera -- may be a "
                "real mount/cable issue; check the mount directly."
            )
    detail = " ".join(notes) if notes else (
        "one axis may not have moved anything real even though the pulse was accepted; "
        "check the mount directly, or try Run Calibration again."
    )
    return f"{_CAMERA_LABELS[camera_key]}: RA-axis and Dec-axis too close to parallel -- {detail}"


def _response_dict(response: AxisResponse) -> dict[str, float]:
    return {
        "dx_px": response.dx_px,
        "dy_px": response.dy_px,
        "magnitude_px": response.magnitude_px,
        "angle_degrees": response.angle_degrees,
    }
