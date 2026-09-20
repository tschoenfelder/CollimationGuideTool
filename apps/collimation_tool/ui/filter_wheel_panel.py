"""FilterWheelPanel — read-only display of an electronic filter wheel's
(EFW) current state. Issue #34.

Far simpler than FocuserPanel: no move/jog controls at all (this issue
is display-only, see astrotool_core.filter_wheel.port's own docstring
for why) -- just a Connect/Disconnect toggle and a QTimer poll loop
rendering FilterWheelState into one status label, plus a
diagnostic_context() contribution (issue's own "Diagnostics" ask).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from astrotool_core.filter_wheel.port import FilterWheelPort, FilterWheelState
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

_POLL_INTERVAL_MS = 250


def _status_text(status: FilterWheelState) -> str:
    if not status.available:
        return f"Filter: {status.reason}" if status.reason else "Filter: unavailable"
    if status.current_slot is None:
        return "Filter: unknown"
    label = (
        f"{status.current_slot} — {status.filter_name}"
        if status.filter_name
        else str(status.current_slot)
    )
    return f"Filter: moving to {label}" if status.moving else f"Filter: {label}"


class FilterWheelPanel(QWidget):
    def __init__(
        self,
        filter_wheel: FilterWheelPort,
        *,
        title: str = "Filter Wheel",
        used_by: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self._filter_wheel = filter_wheel
        #: Issue #41: the optical trains that share this ONE physical wheel.
        self._used_by: tuple[str, ...] = tuple(used_by)
        self._title = title
        self._connected = False

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Filter: not connected")
        self._used_by_label = QLabel(
            f"Used by: {', '.join(self._used_by)}" if self._used_by else ""
        )

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        if self._used_by:
            layout.addWidget(self._used_by_label)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_status)

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
            self._connect_button.setText("Disconnect")
            self._timer.start()
            self._poll_status()
        else:
            self._timer.stop()
            self._filter_wheel.disconnect()
            self._connected = False
            self._connect_button.setText("Connect")
            self._status_label.setText("Filter: not connected")

    def _poll_status(self) -> None:
        if not self._connected:
            return
        self._status_label.setText(_status_text(self._filter_wheel.status()))

    @property
    def title(self) -> str:
        return self._title

    @property
    def used_by(self) -> tuple[str, ...]:
        return self._used_by

    @property
    def connected(self) -> bool:
        return self._connected

    def diagnostic_context(self) -> dict[str, Any]:
        status = self._filter_wheel.status()
        return {
            "available": status.available,
            "current_slot": status.current_slot,
            "filter_name": status.filter_name,
            "moving": status.moving,
            "reason": status.reason,
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected."""
        self._timer.stop()
        if self._connected:
            self._filter_wheel.disconnect()
            self._connected = False
