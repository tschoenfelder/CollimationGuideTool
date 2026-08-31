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
from typing import Any

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

_POLL_INTERVAL_MS = 250
_STEP_SIZES = (1, 5, 10, 50)
_DEFAULT_STEP_SIZE = _STEP_SIZES[0]
#: Safety net for _move_in_flight — see module docstring's "One move at a
#: time". Generous relative to this rig's real settle time (~1-2s, gated
#: by the driver's own 1s polling period) without risking a stuck-disabled
#: button forever if a future driver's move completes faster than this
#: panel's poll interval can ever catch a Busy state.
_MOVE_CONFIRMATION_TIMEOUT_S = 10.0


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

    def __init__(self, focuser: FocuserPort, *, title: str = "Focuser") -> None:
        super().__init__()
        self._focuser = focuser
        self._connected = False
        #: See module docstring's "One move at a time".
        self._move_in_flight = False
        self._seen_busy_since_move = False
        self._move_issued_at: float | None = None

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

        move_row = QHBoxLayout()
        move_row.addWidget(self._in_button)
        move_row.addWidget(self._out_button)
        move_row.addStretch(1)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(step_row)
        layout.addLayout(move_row)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_status)

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
        )
        self._in_button.setEnabled(available)
        self._out_button.setEnabled(available)

    def diagnostic_context(self) -> dict[str, Any]:
        status = self._focuser.status()
        return {
            "available": status.available,
            "position": status.position,
            "max_position": status.max_position,
            "moving": status.moving,
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected."""
        self._timer.stop()
        if self._connected:
            self._focuser.disconnect()
            self._connected = False
