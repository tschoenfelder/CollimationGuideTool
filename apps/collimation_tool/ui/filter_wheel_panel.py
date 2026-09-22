"""FilterWheelPanel — display and (issue #47) commanding of an electronic
filter wheel's (EFW) slot.

Issue #34 shipped read-only status (Connect/Disconnect + one status label +
a QTimer poll loop). Issue #47 adds an optional slot selector, present only
on the panel(s) for the optical train(s) the shared config says the wheel
actually serves right now (`selectable=True`, decided by
`main_window.py` -- see `filter_wheel.config`'s own docstring for where that
fact comes from). A panel constructed with `selectable=False` (the default)
is unchanged from #34's pure status display.

One action at a time, ported from `FocuserPanel`'s own "One move at a time"
idiom (see that module's docstring): the combo+button are disabled
synchronously in the same click handler that issues the command, before the
event loop can deliver a second click, and stay disabled until a genuine
Busy->Ok transition (or a generous safety-net timeout) confirms the move is
actually done -- never inferred from an acknowledgement alone.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any

from astrotool_core.filter_wheel.port import FilterWheelPort, FilterWheelState
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_POLL_INTERVAL_MS = 250
#: Safety net for _slot_change_in_flight -- see FocuserPanel's own
#: _MOVE_CONFIRMATION_TIMEOUT_S docstring for the identical reasoning: never
#: leave the selector stuck disabled forever if a future driver's move
#: completes faster than this panel's poll can ever observe Busy.
_SLOT_CHANGE_CONFIRMATION_TIMEOUT_S = 10.0


def _slot_label(slot: int, name: str | None) -> str:
    return f"{slot} — {name}" if name else str(slot)


def _status_text(status: FilterWheelState, requested_slot: int | None) -> str:
    if not status.available:
        return f"Filter: {status.reason}" if status.reason else "Filter: unavailable"
    if status.current_slot is None:
        return "Filter: unknown"
    label = _slot_label(status.current_slot, status.filter_name)
    if status.moving:
        target = f" (requested {requested_slot})" if requested_slot is not None else ""
        return f"Filter: moving to {label}{target}"
    return f"Filter: {label}"


class FilterWheelPanel(QWidget):
    def __init__(
        self,
        filter_wheel: FilterWheelPort,
        *,
        title: str = "Filter Wheel",
        used_by: Sequence[str] = (),
        selectable: bool = False,
    ) -> None:
        super().__init__()
        self._filter_wheel = filter_wheel
        #: Issue #41: the optical trains that share this ONE physical wheel.
        self._used_by: tuple[str, ...] = tuple(used_by)
        self._title = title
        self._connected = False
        #: Issue #47: whether THIS panel commands the wheel -- decided by
        #: main_window.py from the shared config's active train, not by this
        #: panel itself. False keeps this panel exactly as #34 shipped it.
        self._selectable = selectable
        self._slot_change_in_flight = False
        self._seen_busy_since_request = False
        self._requested_slot: int | None = None
        self._slot_change_issued_at: float | None = None
        self._last_failure: str | None = None

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Filter: not connected")
        self._used_by_label = QLabel(
            f"Used by: {', '.join(self._used_by)}" if self._used_by else ""
        )

        self._slot_combo = QComboBox()
        self._slot_combo.setVisible(self._selectable)
        self._set_button = QPushButton("Set")
        self._set_button.setVisible(self._selectable)
        self._set_button.clicked.connect(self._on_set_clicked)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        selector_row = QHBoxLayout()
        selector_row.addWidget(self._slot_combo)
        selector_row.addWidget(self._set_button)
        selector_row.addStretch(1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        if self._selectable:
            layout.addLayout(selector_row)
        if self._used_by:
            layout.addWidget(self._used_by_label)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_status)

        self._update_selector_enabled()

    def _set_slot_change_in_flight(self, value: bool) -> None:
        self._slot_change_in_flight = value

    def _on_toggle_connect(self, checked: bool) -> None:
        if checked:
            try:
                self._filter_wheel.connect()
            except Exception as exc:  # noqa: BLE001 -- any failure must be shown, never swallowed
                self._status_label.setText(f"Filter: connect failed — {exc}")
                # blockSignals: resetting the button's checked state here
                # must not re-enter this handler with checked=False, which
                # would immediately overwrite the error message above.
                self._connect_button.blockSignals(True)
                self._connect_button.setChecked(False)
                self._connect_button.blockSignals(False)
                return
            self._connected = True
            self._set_slot_change_in_flight(False)
            self._connect_button.setText("Disconnect")
            self._timer.start()
            self._poll_status()
        else:
            self._timer.stop()
            self._filter_wheel.disconnect()
            self._connected = False
            self._set_slot_change_in_flight(False)
            self._connect_button.setText("Connect")
            self._status_label.setText("Filter: not connected")
        self._update_selector_enabled()

    def _refresh_combo(self) -> None:
        """(Re)populate the combo from the wheel's own known slot names,
        preserving whatever is currently selected where possible. Never
        fires _on_set_clicked-adjacent signals -- selecting an entry only
        ever changes what a later "Set" click would send."""
        if not self._selectable or not self._connected:
            return
        names = self._filter_wheel.slot_names()
        status = self._filter_wheel.status()
        slots = sorted(set(names) | ({status.current_slot} if status.current_slot else set()))
        if not slots:
            return
        previous = self._slot_combo.currentData()
        self._slot_combo.blockSignals(True)
        self._slot_combo.clear()
        for slot in slots:
            self._slot_combo.addItem(_slot_label(slot, names.get(slot)), slot)
        if previous is not None:
            index = self._slot_combo.findData(previous)
            if index >= 0:
                self._slot_combo.setCurrentIndex(index)
        self._slot_combo.blockSignals(False)

    def _on_set_clicked(self) -> None:
        if not self._selectable or not self._connected or self._slot_change_in_flight:
            return
        slot = self._slot_combo.currentData()
        if slot is None:
            return
        # Disable synchronously, before issuing the command -- see module
        # docstring's "One action at a time".
        self._set_slot_change_in_flight(True)
        self._seen_busy_since_request = False
        self._requested_slot = slot
        self._slot_change_issued_at = time.monotonic()
        self._last_failure = None
        self._update_selector_enabled()
        try:
            self._filter_wheel.set_slot(slot)
        except Exception as exc:  # noqa: BLE001 -- shown, never swallowed
            self._last_failure = str(exc)
            self._set_slot_change_in_flight(False)
            self._requested_slot = None
            self._status_label.setText(f"Filter: {exc}")
            self._update_selector_enabled()

    def _poll_status(self) -> None:
        if not self._connected:
            return
        self._refresh_combo()
        status = self._filter_wheel.status()
        if self._last_failure is None:
            self._status_label.setText(_status_text(status, self._requested_slot))
        if self._slot_change_in_flight:
            if status.moving:
                self._seen_busy_since_request = True
            elif self._seen_busy_since_request:
                self._set_slot_change_in_flight(False)
                self._requested_slot = None
            elif (
                self._slot_change_issued_at is not None
                and time.monotonic() - self._slot_change_issued_at
                > _SLOT_CHANGE_CONFIRMATION_TIMEOUT_S
            ):
                # Safety net -- see _SLOT_CHANGE_CONFIRMATION_TIMEOUT_S's docstring.
                self._set_slot_change_in_flight(False)
                self._requested_slot = None
        self._update_selector_enabled()

    def _update_selector_enabled(self) -> None:
        available = (
            self._selectable
            and self._connected
            and self._filter_wheel.is_available
            and not self._slot_change_in_flight
        )
        self._slot_combo.setEnabled(available)
        self._set_button.setEnabled(available)

    @property
    def title(self) -> str:
        return self._title

    @property
    def used_by(self) -> tuple[str, ...]:
        return self._used_by

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def selectable(self) -> bool:
        return self._selectable

    def diagnostic_context(self) -> dict[str, Any]:
        status = self._filter_wheel.status()
        return {
            "available": status.available,
            "current_slot": status.current_slot,
            "filter_name": status.filter_name,
            "moving": status.moving,
            "reason": status.reason,
            "requested_slot": self._requested_slot,
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected."""
        self._timer.stop()
        if self._connected:
            self._filter_wheel.disconnect()
            self._connected = False
