"""FocuserPanel — manual jog control for the main optical train's OnStep
focuser, connected via a real indiserver (see
`astrotool_core.focus.indi_focuser_adapter.IndiFocuserAdapter`).

Mirrors `CameraPanel`'s conventions (a `QTimer` poll loop reading cheap
status rather than driving anything expensive inline, a
`diagnostic_context()` contribution) but is simpler: one fixed focuser,
no device picker, no streaming — "Connect" opens the INDI connection,
after which "In"/"Out" buttons jog the focuser by a selectable step size.

Sign convention (see `IndiFocuserAdapter`'s own docstring, since it's
otherwise adapter-arbitrary): positive `move()` steps are outward.

Stop (real incident a4ffe048): "no way to stop" -- a move whose Busy->Ok
transition the driver never confirmed left In/Out permanently disabled
(gated on both `_move_in_flight` and the driver's own possibly-stuck
`is_moving()`), with no recovery besides restarting the app. `stop()` is
a real `FocuserPort` ABC method (`IndiFocuserAdapter.stop()` sends
`FOCUS_ABORT_MOTION`) that was simply never wired to a button here.
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
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import FrameAcquisitionResult
from astrotool_core.focus.port import FocuserPort
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
from collimation_tool.ui.autofocus_runner import AutofocusRunner

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
    ) -> None:
        super().__init__()
        #: Issue #44: returns a reason string when measurement is blocked
        #: (terrestrial mode + mount tracking not OFF), else None.
        self._measurement_gate = measurement_gate
        self._focuser = focuser
        #: Issue #33 (artificial star): lets Auto Focus lower exposure when
        #: the star saturates at focus (restored after every run).
        self._exposure_control = exposure_control
        self._connected = False
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

        # Issue #33: Auto Focus mode toggle + run/cancel + status -- same
        # QButtonGroup convention MountTestMovePanel already established
        # for its own Star/Terrestrial toggle. Star selected by default.
        self._af_mode_group = QButtonGroup(self)
        self._af_mode_group.setExclusive(True)
        self._af_star_button = QPushButton("Star")
        self._af_star_button.setCheckable(True)
        self._af_star_button.setChecked(True)
        self._af_mode_group.addButton(self._af_star_button)
        self._af_terrestrial_button = QPushButton("Terrestrial")
        self._af_terrestrial_button.setCheckable(True)
        self._af_mode_group.addButton(self._af_terrestrial_button)
        # Issue #33 enhancement: ONE artificial star, star-specific metric,
        # same target through the sweep -- distinct from natural-star and
        # terrestrial-scene autofocus.
        self._af_artificial_button = QPushButton("Artificial star")
        self._af_artificial_button.setCheckable(True)
        self._af_mode_group.addButton(self._af_artificial_button)

        self._auto_focus_button = QPushButton("Auto Focus")
        self._auto_focus_button.clicked.connect(self._on_auto_focus_clicked)
        self._auto_focus_cancel_button = QPushButton("Cancel")
        self._auto_focus_cancel_button.clicked.connect(self._on_auto_focus_cancel_clicked)
        self._auto_focus_status_label = QLabel("")

        autofocus_row = QHBoxLayout()
        autofocus_row.addWidget(QLabel("Auto Focus"))
        autofocus_row.addWidget(self._af_star_button)
        autofocus_row.addWidget(self._af_terrestrial_button)
        autofocus_row.addWidget(self._af_artificial_button)
        autofocus_row.addWidget(self._auto_focus_button)
        autofocus_row.addWidget(self._auto_focus_cancel_button)
        autofocus_row.addWidget(self._auto_focus_status_label, stretch=1)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(step_row)
        layout.addLayout(move_row)
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

    def _on_toggle_connect(self, checked: bool) -> None:
        if checked:
            try:
                self._focuser.connect()
            except ConnectionError as exc:
                self._status_label.setText(f"Connect failed — {exc}")
                # blockSignals: resetting the button's checked state here
                # must not re-enter this handler with checked=False, which
                # would immediately overwrite the error message above with
                # "Not connected.".
                self._connect_button.blockSignals(True)
                self._connect_button.setChecked(False)
                self._connect_button.blockSignals(False)
                self._update_move_buttons_enabled()
                return
            self._connected = True
            self._set_move_in_flight(False)
            self._connect_button.setText("Disconnect")
            self._timer.start()
            self._poll_status()
        else:
            self._timer.stop()
            self._focuser.disconnect()
            self._connected = False
            self._set_move_in_flight(False)
            self._connect_button.setText("Connect")
            self._status_label.setText("Not connected.")
        self._update_move_buttons_enabled()

    def _begin_move(self, steps: int) -> None:
        # Disable synchronously, before issuing the move -- see module
        # docstring's "One move at a time". Qt delivers input on this one
        # thread, so a button already disabled here cannot receive a second
        # click before this handler returns.
        self._set_move_in_flight(True)
        self._seen_busy_since_move = False
        self._move_issued_at = time.monotonic()
        self._update_move_buttons_enabled()
        self._focuser.move(steps)

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
        # (FOCUS_ABORT_MOTION) via FocuserPort.stop() -- unlike the mount
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
        controller = AutofocusController(
            self._focuser,
            get_frame=self._get_frame,
            wait_for_frame=self._wait_for_frame,
            set_auto_exposure_paused=self._set_auto_exposure_paused,
            exposure_control=self._exposure_control,
            # Issue #35: same optical train for both -- this app has no
            # independent per-train focuser yet (see module docstring).
            camera_label=self._optical_train_label,
            focuser_label=self._optical_train_label,
        )
        if self._af_artificial_button.isChecked():
            mode = AutofocusMode.ARTIFICIAL_STAR
        elif self._af_terrestrial_button.isChecked():
            mode = AutofocusMode.TERRESTRIAL
        else:
            mode = AutofocusMode.STAR
        started = self._autofocus_runner.submit(controller, mode)
        if not started:
            return  # a run is already in flight
        self._autofocus_running = True
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
        self._auto_focus_status_label.setText(_format_autofocus_status(result))
        self._update_move_buttons_enabled()

    def select_artificial_star_mode(self, selected: bool) -> None:
        """Select (or leave) the artificial-star autofocus mode -- called
        by MainWindow when the collimation target mode changes (issue #39/#33:
        the mode is inferred, not left for the user to work out). Leaving it
        only ever falls back to natural-star mode when artificial-star was the
        active choice; a terrestrial choice is left alone."""
        if selected:
            self._af_artificial_button.setChecked(True)
        elif self._af_artificial_button.isChecked():
            self._af_star_button.setChecked(True)

    def _poll_status(self) -> None:
        if not self._connected:
            return
        status = self._focuser.status()
        if not status.available:
            self._status_label.setText("Connected — no focuser hardware detected.")
        else:
            moving = " (moving…)" if status.moving else ""
            self._status_label.setText(
                f"Position {status.position} / {status.max_position}{moving}"
            )
        if self._move_in_flight:
            if status.moving:
                self._seen_busy_since_move = True
            elif self._seen_busy_since_move:
                self._set_move_in_flight(False)
            elif (
                self._move_issued_at is not None
                and time.monotonic() - self._move_issued_at > _MOVE_CONFIRMATION_TIMEOUT_S
            ):
                # Safety net — see _MOVE_CONFIRMATION_TIMEOUT_S's docstring.
                self._set_move_in_flight(False)
        self._update_move_buttons_enabled()

    def _update_move_buttons_enabled(self) -> None:
        available = (
            self._connected
            and self._focuser.is_available
            and not self._focuser.is_moving()
            and not self._move_in_flight
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
        return {
            "available": status.available,
            "position": status.position,
            "max_position": status.max_position,
            "moving": status.moving,
        }

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
        moving after quitting the app. disconnect() alone only closes the
        INDI client socket; it never sends FOCUS_ABORT_MOTION, so a move
        already in progress on the real hardware would just keep running
        with nothing left to stop it (same underlying gap the Stop button
        exists for, see this module's own docstring's "Stop" section --
        just hit on quit instead of a click)."""
        self._timer.stop()
        self._autofocus_poll_timer.stop()
        if self._autofocus_running:
            self._autofocus_runner.cancel()
        if self._connected:
            self._focuser.stop()
            self._focuser.disconnect()
            self._connected = False
