"""FocuserPanel — manual jog control for the main optical train's OnStep
focuser, reached only through OnStepAdapter (see
`astrotool_core.onstep.OnStepFocuserAdapter`).

Mirrors `CameraPanel`'s conventions (a `QTimer` poll loop reading cheap
status rather than driving anything expensive inline, a
`diagnostic_context()` contribution) but is simpler: one fixed focuser,
no device picker, no streaming — "Connect" opens the shared OnStep connection,
after which "In"/"Out" buttons jog the focuser by a selectable step size.

Sign convention (OnStepAdapter's convention): positive `move()` steps are outward.

Stop (real incident a4ffe048): "no way to stop" -- a move whose Busy->Ok
transition the driver never confirmed left In/Out permanently disabled
(gated on both `_move_in_flight` and the driver's own possibly-stuck
`is_moving()`), with no recovery besides restarting the app. `stop()` is
a real `FocuserPort` ABC method (`OnStepFocuserAdapter.stop()` calls OnStepAdapter's
focuser stop) that was simply never wired to a button here.
Always enabled once connected, regardless of the (possibly wrong)
in-flight/moving state -- that's the whole point of an abort control.

One move at a time (issue #87349fd3): a second relative move issued to
the real OnStep INDI driver *while the first is still in flight* was
found, on real hardware, to silently corrupt the result -- e.g. two
rapid `move(50)` calls landed only 50 steps out, not 100, with no error
of any kind. The driver's own async status (`is_moving()`) lags a real
click by tens of milliseconds before it first reports Busy, so relying
on it alone to disable the buttons leaves a real window in which a fast
second click (or double-click) reaches the driver before the first
move's Busy state was ever observed. `_move_in_flight` closes that
window by disabling In/Out synchronously, in the same click handler that
issues the move -- before the event loop can ever deliver a second
click -- and keeps them disabled until a genuine Busy→Ok transition (or
a generous timeout, in case a future driver settles too fast for this
panel's poll to ever observe Busy at all) confirms the move is actually
done.

Jog off the GUI thread (S6.8c, R7): OnStepAdapter's focuser move blocks until the target is
confirmed (up to 30 s), so the jog runs on a background `OperationLifecycle` ("focuser-jog",
the S6.3 pattern: one at a time, guaranteed terminal state) and the click handler returns at
once. In/Out (and Auto Focus) stay disabled while that worker still runs, also after a Stop
dropped the in-flight window -- one move at a time, as above. Stop reaches the running move
because `OnStepFocuserAdapter.stop()` no longer queues behind it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import FrameAcquisitionResult
from astrotool_core.focus.port import FocuserPort
from astrotool_core.mount.operating_mode import OperatingMode
from astrotool_core.session import ConnectionFailure, DeviceConnectionService
from astrotool_core.timing import SYSTEM_CLOCK, Clock
from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.application.autofocus_controller import (
    AutofocusController,
    AutofocusMode,
    AutofocusResult,
    ExposureControl,
)
from collimation_tool.application.autofocus_search import AutofocusStatus
from collimation_tool.application.operation_lifecycle import (
    CompletionVerdict,
    OperationLifecycle,
    StatusPollGuard,
    completion_verdict,
)
from collimation_tool.ui.autofocus_runner import AutofocusRunner
from collimation_tool.ui.flow_layout import FlowLayout

_POLL_INTERVAL_MS = 250
_STEP_SIZES = (1, 10, 100, 200)
_DEFAULT_STEP_SIZE = _STEP_SIZES[0]
#: Safety net for _move_in_flight — see module docstring's "One move at a
#: time". Generous relative to this rig's real settle time (~1-2s, gated
#: by the driver's own 1s polling period) without risking a stuck-disabled
#: button forever if a future driver's move completes faster than this
#: panel's poll interval can ever catch a Busy state.
_MOVE_CONFIRMATION_TIMEOUT_S = 10.0
_AUTOFOCUS_POLL_INTERVAL_MS = 200
#: S6.8c: on quit, how long (real time, a thread join) the jog worker may take to end after
#: the focuser stop before the connection is released -- short; the release goes on anyway.
_QUIT_JOG_WAIT_S = 3.0

_log = logging.getLogger(__name__)


def _format_autofocus_status(result: AutofocusResult) -> str:
    mode_text = result.mode.value.replace("_", " ")
    prefix = f"Auto focus ({result.optical_train})"
    if result.status is AutofocusStatus.SUCCESS:
        return (
            f"{prefix}: {mode_text} best {result.best_position} "
            f"(confidence {result.confidence:.2f})"
        )
    if result.status is AutofocusStatus.ALREADY_FOCUSED:
        return (
            f"{prefix}: {mode_text} -- already focused at {result.best_position} "
            f"(validated on a fresh frame)"
        )
    text = f"{prefix}: {result.status.value} ({mode_text})"
    if result.failure_reason in ("saturated", "saturated_at_min_exposure"):
        return f"{text} -- star saturated: lower exposure/gain and retry"
    if result.failure_reason:
        return f"{text} -- {result.failure_reason}"
    return text


class FocuserPanel(QWidget):
    #: Fires whenever "one move at a time"'s own in-flight window opens
    #: or closes -- issued (or resumed after connect/disconnect) True,
    #: confirmed-done (or the safety-net timeout) False. MainWindow wires
    #: this to the paired camera panel's `set_updates_paused()` so a jog
    #: doesn't drive live analysis off a frame captured mid-move. Fires on
    #: the issuing click, not the driver's own (laggy) first Busy report --
    #: see module docstring's "One move at a time" for why relying on
    #: Busy alone leaves a real window where physical motion has already
    #: started.
    move_in_flight_changed = Signal(bool)

    def __init__(
        self,
        focuser: FocuserPort,
        *,
        title: str = "Focuser",
        get_frame: Callable[[], np.ndarray | None] | None = None,
        wait_for_frame: Callable[[float, float], FrameAcquisitionResult] | None = None,
        set_auto_exposure_paused: Callable[[bool], None] | None = None,
        optical_train_label: str = "Main",
        exposure_control: ExposureControl | None = None,
        measurement_gate: Callable[[str], str | None] | None = None,
        clock: Clock | None = None,
        operating_mode: Callable[[], OperatingMode] | None = None,
    ) -> None:
        super().__init__()
        #: Issue #48 / user decision M04: the global operating mode's single owner (read live).
        #: It selects the autofocus metric -- Terrestrial -> Tenengrad, Astronomical ->
        #: star/FWHM; this panel has no Star/Terrestrial selector of its own. None (standalone,
        #: no app-wide mode wired): the fixed legacy default, star/FWHM.
        self._operating_mode = operating_mode
        #: Set when a mode change cancelled a running autofocus (shown with its result).
        self._cancelled_by_mode_change = False
        #: Issue #53 (S3b): the time the confirmation-timeout safety net reads. The Qt
        #: poll timer stays the tick source; tests inject a `FakeClock`.
        self._clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        #: Issue #44: returns a reason string when measurement is blocked
        #: (terrestrial mode + mount tracking not OFF), else None.
        self._measurement_gate = measurement_gate
        self._focuser = focuser
        #: Issue #33 (artificial star): lets Auto Focus lower exposure when
        #: the star saturates at focus (restored after every run).
        self._exposure_control = exposure_control
        #: #52 K01/K03 (S6.2): connect/disconnect lifecycle, failure reporting (one log line,
        #: `last_connect_error` -- diagnostic e1ddabca) and guaranteed release. WAIT = the first
        #: status read; CANCEL = the motion abort on quit (the focuser kept moving after quit).
        self._connection = DeviceConnectionService(
            title, focuser, wait=lambda: focuser.status(), cancel=lambda: focuser.stop()
        )
        #: S6.3 (#52 L04): the periodic status poll never raises into Qt.
        self._poll_guard = StatusPollGuard(title)
        # Issue #35: this panel is wired to exactly one optical train
        # today (see module docstring) -- carried through into Auto
        # Focus's own status text and diagnostic evidence so that fact
        # is visible/recorded, not just implicit in how MainWindow
        # happens to construct this panel.
        self._optical_train_label = optical_train_label
        #: See module docstring's "One move at a time".
        self._move_in_flight = False
        self._seen_busy_since_move = False
        self._move_issued_at: float | None = None
        #: S6.8c: the jog's blocking move runs here, never on the GUI thread.
        self._jog: OperationLifecycle[None] = OperationLifecycle("focuser-jog")
        self._jog_thread: threading.Thread | None = None

        # Issue #33: Auto Focus needs camera access this panel otherwise
        # has none of -- optional, injectable-default (None) so existing
        # manual-jog-only construction/tests keep working unchanged when
        # unsupplied (see _update_move_buttons_enabled's own gating).
        self._get_frame = get_frame
        self._wait_for_frame = wait_for_frame
        self._set_auto_exposure_paused = set_auto_exposure_paused
        self._autofocus_runner = AutofocusRunner()
        self._autofocus_running = False
        self._last_autofocus_result: AutofocusResult | None = None

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")

        self._step_group = QButtonGroup(self)
        self._step_group.setExclusive(True)
        step_row = QHBoxLayout()
        step_row.addWidget(QLabel("Step"))
        for step in _STEP_SIZES:
            button = QPushButton(str(step))
            button.setCheckable(True)
            button.setChecked(step == _DEFAULT_STEP_SIZE)
            self._step_group.addButton(button, step)
            step_row.addWidget(button)
        step_row.addStretch(1)

        self._in_button = QPushButton("◄ In")
        self._in_button.clicked.connect(self._on_move_in)
        self._out_button = QPushButton("Out ►")
        self._out_button.clicked.connect(self._on_move_out)
        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self._on_stop)

        move_row = QHBoxLayout()
        move_row.addWidget(self._in_button)
        move_row.addWidget(self._out_button)
        move_row.addWidget(self._stop_button)
        move_row.addStretch(1)

        # Issue #48 / M04: no Star/Terrestrial selector -- the metric follows the global
        # Operating mode; this read-only line says which one it selects.
        self._af_metric_label = QLabel("")
        self._af_metric_label.setWordWrap(True)
        # Issue #33 enhancement: ONE artificial star, star-specific metric,
        # same target through the sweep -- a TARGET TYPE (orthogonal to the
        # operating mode, AGENTS.md), set from the collimation target mode.
        self._af_artificial_button = QPushButton("Artificial star")
        self._af_artificial_button.setCheckable(True)
        self._af_artificial_button.toggled.connect(lambda _checked: self._show_af_metric())

        self._auto_focus_button = QPushButton("Auto Focus")
        self._auto_focus_button.clicked.connect(self._on_auto_focus_clicked)
        self._auto_focus_cancel_button = QPushButton("Cancel")
        self._auto_focus_cancel_button.clicked.connect(self._on_auto_focus_cancel_clicked)
        self._auto_focus_status_label = QLabel("")
        self._auto_focus_status_label.setWordWrap(True)

        # Field report (2026-09-29): the same class of bug found in
        # MountTestMovePanel/MainWindow's calibration row -- six items
        # (label + 5 buttons) sharing one row exceeded the Focus & Filters
        # tab's viewport width, clipping content off the visible edge. A
        # hand-tuned fixed-row split (two rows) fixed it for the platforms
        # tested against at the time, but still clipped on the real Pi
        # screen's own font metrics -- FlowLayout wraps by construction at
        # whatever width it's actually given instead.
        autofocus_mode_row = FlowLayout()
        autofocus_mode_row.addWidget(QLabel("Auto Focus"))
        autofocus_mode_row.addWidget(self._af_artificial_button)
        autofocus_mode_row.addWidget(self._af_metric_label)
        self._show_af_metric()

        autofocus_row = FlowLayout()
        autofocus_row.addWidget(self._auto_focus_button)
        autofocus_row.addWidget(self._auto_focus_cancel_button)
        autofocus_row.addWidget(self._auto_focus_status_label)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(step_row)
        layout.addLayout(move_row)
        layout.addLayout(autofocus_mode_row)
        layout.addLayout(autofocus_row)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_status)

        self._autofocus_poll_timer = QTimer(self)
        self._autofocus_poll_timer.setInterval(_AUTOFOCUS_POLL_INTERVAL_MS)
        self._autofocus_poll_timer.timeout.connect(self._poll_autofocus)

        self._update_move_buttons_enabled()

    def _set_move_in_flight(self, value: bool) -> None:
        if value == self._move_in_flight:
            return
        self._move_in_flight = value
        self.move_in_flight_changed.emit(value)

    def _selected_step(self) -> int:
        step_id = self._step_group.checkedId()
        return step_id if step_id != -1 else _DEFAULT_STEP_SIZE

    @property
    def _connected(self) -> bool:
        return self._connection.connected

    def _on_toggle_connect(self, checked: bool) -> None:
        if checked:
            failure = self._connection.connect()
            if failure is not None:
                self._show_connection_failure(failure)
                return
            self._set_move_in_flight(False)
            self._connect_button.setText("Disconnect")
            self._timer.start()
            self._poll_status()
        else:
            self._timer.stop()
            failures = self._connection.disconnect()
            self._set_move_in_flight(False)
            self._connect_button.setText("Connect")
            self._status_label.setText(failures[-1].status_text if failures else "Not connected.")
        self._update_move_buttons_enabled()

    def _show_connection_failure(self, failure: ConnectionFailure) -> None:
        self._status_label.setText(failure.status_text)
        # blockSignals: resetting the button's checked state here must not re-enter
        # this handler with checked=False, which would overwrite the reason with
        # "Not connected.".
        self._connect_button.blockSignals(True)
        self._connect_button.setChecked(False)
        self._connect_button.blockSignals(False)
        self._update_move_buttons_enabled()

    def _begin_move(self, steps: int) -> None:
        if self._jog.is_busy:
            return  # the previous jog's move is still ending (one move at a time)
        # Disable synchronously, before issuing the move -- see module
        # docstring's "One move at a time". Qt delivers input on this one
        # thread, so a button already disabled here cannot receive a second
        # click before this handler returns.
        self._set_move_in_flight(True)
        self._seen_busy_since_move = False
        self._move_issued_at = self._clock.monotonic()
        self._update_move_buttons_enabled()
        focuser = self._focuser
        # S6.8c: off the GUI thread; a failure is logged by the lifecycle, the poll and the
        # confirmation safety net end the in-flight window as before.
        self._jog.start(
            lambda: focuser.move(steps),
            on_error=lambda _exc: None,
            publish=lambda _none: None,
            spawn=self._spawn_jog,
        )

    def _spawn_jog(self, target: Callable[[], None]) -> None:
        thread = threading.Thread(target=target, daemon=True, name="focuser-jog")
        self._jog_thread = thread  # quit joins it (bounded)
        thread.start()

    def _on_move_in(self) -> None:
        self._begin_move(-self._selected_step())

    def _on_move_out(self) -> None:
        self._begin_move(self._selected_step())

    def _on_stop(self) -> None:
        # Real incident a4ffe048: a move that never confirmed Busy->Ok
        # (is_moving() stuck reporting True, position never updating)
        # left In/Out permanently disabled with no way to recover -- this
        # is that panel's own missing escape hatch, same reasoning as
        # MountTestMovePanel's Stop button. Sends the real hardware abort
        # via FocuserPort.stop() -- unlike the mount
        # adapter's abort(), this is a real FocuserPort ABC method, no
        # duck-typing needed. Also drops this panel's own in-flight
        # tracking immediately, so a stuck safety-net timeout isn't the
        # only way back -- but does *not* override is_moving() itself:
        # if the driver genuinely never clears Busy after the abort,
        # In/Out staying disabled reflects a real, separate hardware/
        # firmware question, not something this click can respond to.
        if not self._connected:
            return
        if self._autofocus_running:
            self._autofocus_runner.cancel()
        self._focuser.stop()
        self._set_move_in_flight(False)
        self._seen_busy_since_move = False
        self._move_issued_at = None
        self._update_move_buttons_enabled()

    def _autofocus_available(self) -> bool:
        return (
            self._connected
            and self._focuser.is_available
            and not self._move_in_flight
            and not self._jog.is_busy
            and not self._autofocus_running
            and self._get_frame is not None
            and self._wait_for_frame is not None
            and self._set_auto_exposure_paused is not None
        )

    def _on_auto_focus_clicked(self) -> None:
        if not self._autofocus_available():
            return
        assert self._get_frame is not None
        assert self._wait_for_frame is not None
        assert self._set_auto_exposure_paused is not None
        if self._measurement_gate is not None:
            blocked = self._measurement_gate("autofocus")
            if blocked:
                self._auto_focus_status_label.setText(f"Auto focus blocked — {blocked}")
                return
        # S6.6b: the camera exposure snapshotted HERE, on the GUI thread, scales the run's first
        # fresh-frame wait (a long exposure is waited for, not timed out); the worker never
        # reads a widget.
        exposure_s = (
            self._exposure_control.get()[0] / 1000.0 if self._exposure_control is not None else None
        )
        controller = AutofocusController(
            self._focuser,
            exposure_hint=lambda: exposure_s,
            get_frame=self._get_frame,
            wait_for_frame=self._wait_for_frame,
            set_auto_exposure_paused=self._set_auto_exposure_paused,
            exposure_control=self._exposure_control,
            # Issue #35: same optical train for both -- this app has no
            # independent per-train focuser yet (see module docstring).
            camera_label=self._optical_train_label,
            focuser_label=self._optical_train_label,
        )
        started = self._autofocus_runner.submit(controller, self._autofocus_mode())
        if not started:
            return  # a run is already in flight
        self._autofocus_running = True
        self._cancelled_by_mode_change = False
        self._auto_focus_status_label.setText(f"Auto focusing ({self._optical_train_label})…")
        self._update_move_buttons_enabled()
        self._autofocus_poll_timer.start()

    def _on_auto_focus_cancel_clicked(self) -> None:
        self._autofocus_runner.cancel()

    def _poll_autofocus(self) -> None:
        outcome = self._autofocus_runner.take_latest()
        if outcome is None:
            return
        self._autofocus_poll_timer.stop()
        self._autofocus_running = False
        result = outcome.result
        self._last_autofocus_result = result
        text = _format_autofocus_status(result)
        if self._cancelled_by_mode_change:
            text += " (cancelled: the operating mode changed during the run)"
            self._cancelled_by_mode_change = False
        self._auto_focus_status_label.setText(text)
        self._update_move_buttons_enabled()

    def _autofocus_mode(self) -> AutofocusMode:
        """Issue #48 / M04: the artificial-star target type when selected, otherwise the
        metric the global operating mode selects (Terrestrial -> Tenengrad, else star/FWHM)."""
        if self._af_artificial_button.isChecked():
            return AutofocusMode.ARTIFICIAL_STAR
        mode = self._operating_mode() if self._operating_mode is not None else None
        if mode is OperatingMode.TERRESTRIAL:
            return AutofocusMode.TERRESTRIAL
        return AutofocusMode.STAR

    def _show_af_metric(self) -> None:
        mode = self._autofocus_mode()
        if mode is AutofocusMode.ARTIFICIAL_STAR:
            text = "Metric: artificial star (single star, FWHM)"
        elif mode is AutofocusMode.TERRESTRIAL:
            text = "Metric: Tenengrad (terrestrial scene) -- follows the Operating mode"
        else:
            text = "Metric: star FWHM (astronomical) -- follows the Operating mode"
        self._af_metric_label.setText(text)

    def operating_mode_changed(self) -> None:
        """Issue #48 / M04: the global mode changed -- the metric follows at once. A run still
        in flight was started under the previous mode (metric, tracking) and is cancelled."""
        self._show_af_metric()
        if self._autofocus_running:
            self._cancelled_by_mode_change = True
            self._autofocus_runner.cancel()

    def select_artificial_star_mode(self, selected: bool) -> None:
        """Select (or leave) the artificial-star autofocus target type -- called
        by MainWindow when the collimation target mode changes (issue #39/#33:
        the mode is inferred, not left for the user to work out). Leaving it
        falls back to the metric the global operating mode selects (#48/M04)."""
        self._af_artificial_button.setChecked(selected)

    def _poll_status(self) -> None:
        """One QTimer tick. S6.3 (#52 L04): never raises into Qt -- a failing status read is
        shown, logged once per failure streak, and the move safety net keeps running."""
        if not self._connected:
            return
        failure = self._poll_guard.run(self._poll_once)
        if failure is not None:
            self._status_label.setText(failure)
            self._observe_move(busy=None)
            self._in_button.setEnabled(False)
            self._out_button.setEnabled(False)
            self._auto_focus_button.setEnabled(False)
            # Stop stays the escape hatch (real incident a4ffe048).
            self._stop_button.setEnabled(True)
            self._auto_focus_cancel_button.setEnabled(self._autofocus_running)

    def _poll_once(self) -> None:
        status = self._focuser.status()
        if not status.available:
            self._status_label.setText("Connected — no focuser hardware detected.")
        else:
            moving = " (moving…)" if status.moving else ""
            self._status_label.setText(
                f"Position {status.position} / {status.max_position}{moving}"
            )
        self._observe_move(busy=status.moving)
        self._update_move_buttons_enabled()

    def _observe_move(self, *, busy: bool | None) -> None:
        """The in-flight window of the last jog (S6.3: `completion_verdict`, the one rule of
        the one-action-at-a-time panels). The safety-net timeout fires even while the driver
        still reports Busy (3fc09ae) -- that ends THIS window (camera updates resume), but
        In/Out stay disabled for as long as the driver itself reports Busy (see
        `_update_move_buttons_enabled` and `_on_stop`); Stop is always the way out."""
        if not self._move_in_flight:
            return
        self._seen_busy_since_move, verdict = completion_verdict(
            busy=busy,
            seen_busy=self._seen_busy_since_move,
            issued_at=self._move_issued_at,
            now=self._clock.monotonic(),
            timeout_s=_MOVE_CONFIRMATION_TIMEOUT_S,
        )
        if verdict is not CompletionVerdict.PENDING:
            self._set_move_in_flight(False)

    def _update_move_buttons_enabled(self) -> None:
        available = (
            self._connected
            and self._focuser.is_available
            and not self._focuser.is_moving()
            and not self._move_in_flight
            and not self._jog.is_busy
            and not self._autofocus_running
        )
        self._in_button.setEnabled(available)
        self._out_button.setEnabled(available)
        # Deliberately not gated on is_moving()/_move_in_flight -- the
        # whole point is to still be clickable exactly when those are
        # stuck (real incident a4ffe048). Just needs a real connection.
        self._stop_button.setEnabled(self._connected and self._focuser.is_available)
        self._auto_focus_button.setEnabled(self._autofocus_available())
        self._auto_focus_cancel_button.setEnabled(self._autofocus_running)

    def diagnostic_context(self) -> dict[str, Any]:
        status = self._focuser.status()
        context: dict[str, Any] = {
            "available": status.available,
            "position": status.position,
            "max_position": status.max_position,
            "moving": status.moving,
            "last_connect_error": self._connection.last_connect_error,
            "connection": self._connection.diagnostic_context(),
            # Issue #48: what the next Auto Focus would use, derived from the global mode
            # and the artificial-star target type (reported separately by the main window).
            "autofocus_mode": self._autofocus_mode().value,
        }
        # OnStepFocuserAdapter-only diagnostic extra (duck-typed, same
        # convention as MountParkPanel's confirm_home/home_confirmed) --
        # FocuserPort itself stays hardware-neutral. Field report: a
        # move_rejected failure with no prior log line needs to show
        # whatever precondition (e.g. focuser_position_stale) was already
        # blocking the move *before* it was even attempted.
        if hasattr(self._focuser, "blockers"):
            context["blockers"] = list(self._focuser.blockers())
        return context

    def diagnostic_autofocus_evidence(self) -> dict[str, Any]:
        """Issue #33's own UUID-diagnostics ask: the full focus curve/
        status/confidence from the most recent Auto Focus run -- same
        "empty dict when there's nothing to report" convention as
        MountTestMovePanel's diagnostic_stability_evidence()/
        diagnostic_backlash_evidence(), cached from the most recent run,
        never cleared by a later one still in flight."""
        if self._last_autofocus_result is None:
            return {}
        result = self._last_autofocus_result
        return {
            "status": result.status.value,
            "mode": result.mode.value,
            # Issue #35: which camera/optical-train/focuser this run
            # actually used -- previously entirely absent from
            # diagnostics, the exact gap that made bundle
            # 73a007b6-6c9b-41e2-a3e5-66a21ec71ffd hard to investigate.
            "camera_label": result.camera_label,
            "optical_train": result.optical_train,
            "focuser_label": result.focuser_label,
            "start_position": result.start_position,
            "best_position": result.best_position,
            "search_min": result.search_min,
            "search_max": result.search_max,
            "confidence": result.confidence,
            "final_value": result.final_value,
            # Issue #33 (artificial star): why no result, the followed target,
            # the start metric, and each exposure attempt.
            "failure_reason": result.failure_reason,
            "tracked_target": result.tracked_target,
            "start_value": result.start_value,
            "exposure_attempts": [list(a) for a in result.exposure_attempts],
            "samples": [
                {
                    "position": point.position,
                    "value": point.value,
                    "confidence": point.confidence,
                    "valid": point.valid,
                    "reason": point.reason,
                }
                for point in result.samples
            ],
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected.

        Aborts any in-flight motion first -- real report: the focuser kept
        moving after quitting the app. disconnect() alone only releases the
        OnStep connection; it never sends an abort, so a move
        already in progress on the real hardware would just keep running
        with nothing left to stop it (same underlying gap the Stop button
        exists for, see this module's own docstring's "Stop" section --
        just hit on quit instead of a click)."""
        self._timer.stop()
        self._autofocus_poll_timer.stop()
        if self._autofocus_running:
            self._autofocus_runner.cancel()
        jog = self._jog_thread
        if self._connected and self._jog.is_busy and jog is not None:
            # S6.8c: stop the jog's move first and let its worker end (bounded) before the
            # release, so the connection is not closed under a running move.
            try:
                self._focuser.stop()
            except Exception:  # noqa: BLE001 -- #52 K03: nothing raises out of quit
                _log.exception("FocuserPanel: stopping the jog on quit failed")
            jog.join(_QUIT_JOG_WAIT_S)
        # #52 K03: a failing abort never skips the release, and nothing raises out of quit.
        self._connection.disconnect(cancel_motion=True)
