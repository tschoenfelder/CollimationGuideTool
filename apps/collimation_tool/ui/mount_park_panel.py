"""MountParkPanel — park/unpark-only control for the OnStep mount,
connected via a real indiserver (see
`astrotool_core.mount.indi_mount_park_adapter.IndiMountParkAdapter`).

Mirrors `FocuserPanel`'s conventions (a `QTimer` poll loop,
`diagnostic_context()`, and the same "one action at a time" click
protection — see that module's "One move at a time" docstring section
for why: a slow, real hardware transition (there, a focuser move; here,
a park/unpark slew) needs its buttons disabled the instant a click
issues it, not only once the driver's own async status catches up, or a
second click can race the first). Deliberately minimal, matching
`MountParkPort`'s own scope — Park and Unpark only, nothing else.
"""

from __future__ import annotations

import time
from typing import Any

from astrotool_core.mount.park_port import MountParkPort
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

_POLL_INTERVAL_MS = 250
#: See FocuserPanel's identical constant for the reasoning — a safety net
#: in case a transition settles faster than this panel could ever catch
#: a Busy state, so the buttons don't stay disabled forever.
_TRANSITION_CONFIRMATION_TIMEOUT_S = 30.0


class MountParkPanel(QWidget):
    def __init__(self, mount: MountParkPort, *, title: str = "Mount") -> None:
        super().__init__()
        self._mount = mount
        self._connected = False
        #: See module docstring's "one action at a time".
        self._action_in_flight = False
        self._pending_action: str | None = None  # "park" | "unpark" | None
        self._seen_busy_since_action = False
        self._action_issued_at: float | None = None

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")

        self._park_button = QPushButton("Park")
        self._park_button.clicked.connect(self._on_park)
        self._unpark_button = QPushButton("Unpark")
        self._unpark_button.clicked.connect(self._on_unpark)

        action_row = QHBoxLayout()
        action_row.addWidget(self._park_button)
        action_row.addWidget(self._unpark_button)
        action_row.addStretch(1)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(action_row)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_status)

        self._update_buttons_enabled()

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
            self._action_in_flight = False
            self._pending_action = None
            self._connect_button.setText("Disconnect")
            self._timer.start()
            self._poll_status()
        else:
            self._timer.stop()
            self._mount.disconnect()
            self._connected = False
            self._action_in_flight = False
            self._pending_action = None
            self._connect_button.setText("Connect")
            self._status_label.setText("Not connected.")
        self._update_buttons_enabled()

    def _begin_action(self, action: str) -> None:
        # Disable synchronously, before issuing the action — see module
        # docstring. Qt delivers input on this one thread, so a button
        # already disabled here cannot receive a second click before
        # this handler returns.
        self._action_in_flight = True
        self._pending_action = action
        self._seen_busy_since_action = False
        self._action_issued_at = time.monotonic()
        self._update_buttons_enabled()
        if action == "park":
            self._mount.park()
        else:
            self._mount.unpark()

    def _on_park(self) -> None:
        self._begin_action("park")

    def _on_unpark(self) -> None:
        self._begin_action("unpark")

    def _poll_status(self) -> None:
        if not self._connected:
            return
        status = self._mount.status()
        if not status.available:
            self._status_label.setText("Connected — no mount interface detected.")
        else:
            state = "Parked" if status.parked else "Unparked"
            tracking = "tracking" if status.tracking else "not tracking"
            self._status_label.setText(f"{state}, {tracking}")
        if self._action_in_flight:
            # "Busy" here means "not yet settled at the target state" --
            # still parked right after an unpark request, or vice versa.
            target_parked = self._pending_action == "park"
            busy = status.available and status.parked != target_parked
            if busy:
                self._seen_busy_since_action = True
            elif self._seen_busy_since_action:
                self._action_in_flight = False
                self._pending_action = None
            elif (
                self._action_issued_at is not None
                and time.monotonic() - self._action_issued_at > _TRANSITION_CONFIRMATION_TIMEOUT_S
            ):
                # Safety net — see _TRANSITION_CONFIRMATION_TIMEOUT_S's docstring.
                self._action_in_flight = False
                self._pending_action = None
        self._update_buttons_enabled()

    def _update_buttons_enabled(self) -> None:
        if not (self._connected and self._mount.is_available) or self._action_in_flight:
            self._park_button.setEnabled(False)
            self._unpark_button.setEnabled(False)
            return
        parked = self._mount.status().parked
        self._park_button.setEnabled(not parked)
        self._unpark_button.setEnabled(parked)

    def diagnostic_context(self) -> dict[str, Any]:
        status = self._mount.status()
        return {
            "available": status.available,
            "parked": status.parked,
            "tracking": status.tracking,
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected."""
        self._timer.stop()
        if self._connected:
            self._mount.disconnect()
            self._connected = False
