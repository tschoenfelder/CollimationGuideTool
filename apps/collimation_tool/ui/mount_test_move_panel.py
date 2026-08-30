"""MountTestMovePanel — axis-calibration diagnostic: pulse the mount
briefly in one direction and measure how far a star moved in each
camera's frame, to learn which physical axis/direction corresponds to
which direction in the picture.

Own `MountPort` connection, separate from `MountParkPanel`'s
`MountParkPort` connection to the same device (same pattern as
`IndiFocuserAdapter`/`IndiMountParkAdapter` already being two independent
`IndiClient` sockets to one INDI device) — see
`astrotool_core.mount.indi_mount_pulse_adapter`'s docstring for the real
INDI properties this drives (`TELESCOPE_SLEW_RATE` fixed at its "20x"
preset, `TELESCOPE_MOTION_NS`/`_WE` for direction).

Gated on the mount being parked (`get_parked` constructor param, a plain
callable rather than coupling this panel to `MountParkPanel`/
`MountParkPort` directly) — a deliberate safety precondition for this
diagnostic action, requested alongside the feature itself, not something
`IndiMountPulseAdapter` enforces on its own.

The actual pulse-and-measure work runs on `MountTestMoveRunner`'s
background thread (see that module's docstring for why) — this panel
just submits, polls for the result, and renders it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrotool_core.mount.axis_calibration import AxisResponse
from astrotool_core.mount.port import AxisDirection, MountAxis, MountPort
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from collimation_tool.ui.mount_test_move_runner import (
    FrameGetter,
    MountTestMoveOutcome,
    MountTestMoveRunner,
)

_POLL_INTERVAL_MS = 250
_PULSE_MS = 500

#: Direction button order/ids -- see IndiMountPulseAdapter's own
#: docstring for the (axis, direction) <-> compass-direction convention
#: this matches (AXIS1=RA/azimuth east/west, AXIS2=Dec/altitude
#: north/south).
_DIRECTIONS: tuple[tuple[str, MountAxis, AxisDirection], ...] = (
    ("N", MountAxis.AXIS2, AxisDirection.POSITIVE),
    ("S", MountAxis.AXIS2, AxisDirection.NEGATIVE),
    ("E", MountAxis.AXIS1, AxisDirection.POSITIVE),
    ("W", MountAxis.AXIS1, AxisDirection.NEGATIVE),
)


class MountTestMovePanel(QWidget):
    def __init__(
        self,
        mount: MountPort,
        *,
        get_parked: Callable[[], bool],
        get_left_frame: FrameGetter,
        get_right_frame: FrameGetter,
        title: str = "Test Move",
        runner: MountTestMoveRunner | None = None,
    ) -> None:
        super().__init__()
        self._mount = mount
        self._get_parked = get_parked
        self._get_left_frame = get_left_frame
        self._get_right_frame = get_right_frame
        self._runner = runner if runner is not None else MountTestMoveRunner()
        self._connected = False
        self._last_outcome: MountTestMoveOutcome | None = None

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")

        self._direction_group = QButtonGroup(self)
        self._direction_group.setExclusive(True)
        direction_row = QHBoxLayout()
        direction_row.addWidget(QLabel("Direction"))
        for index, (label, _axis, _direction) in enumerate(_DIRECTIONS):
            button = QPushButton(label)
            button.setCheckable(True)
            button.setChecked(index == 0)
            self._direction_group.addButton(button, index)
            direction_row.addWidget(button)
        direction_row.addStretch(1)

        self._test_move_button = QPushButton(f"Test Move ({_PULSE_MS}ms, 20x)")
        self._test_move_button.clicked.connect(self._on_test_move)

        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(direction_row)
        layout.addWidget(self._test_move_button)
        layout.addWidget(self._result_label)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)

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

    def _selected_direction(self) -> tuple[MountAxis, AxisDirection]:
        index = self._direction_group.checkedId()
        _label, axis, direction = _DIRECTIONS[index]
        return axis, direction

    def _on_test_move(self) -> None:
        axis, direction = self._selected_direction()
        started = self._runner.submit(
            self._mount, axis, direction, _PULSE_MS, self._get_left_frame, self._get_right_frame
        )
        if not started:
            return  # a test move is already running
        self._result_label.setText("Testing…")
        self._update_buttons_enabled()

    def _poll(self) -> None:
        if not self._connected:
            return
        outcome = self._runner.take_latest()
        if outcome is not None:
            self._last_outcome = outcome
            self._result_label.setText(_format_outcome(outcome))
        self._update_buttons_enabled()

    def _update_buttons_enabled(self) -> None:
        can_test = self._connected and self._get_parked() and not self._runner.is_busy
        self._test_move_button.setEnabled(can_test)

    def diagnostic_context(self) -> dict[str, Any]:
        if self._last_outcome is None:
            return {"last_result": None}
        if self._last_outcome.error is not None:
            return {"last_result": {"error": self._last_outcome.error}}
        return {
            "last_result": {
                key: {
                    "dx_px": response.dx_px,
                    "dy_px": response.dy_px,
                    "magnitude_px": response.magnitude_px,
                    "angle_degrees": response.angle_degrees,
                }
                for key, response in self._last_outcome.responses.items()
            }
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected."""
        self._timer.stop()
        if self._connected:
            self._mount.disconnect()
            self._connected = False


def _format_outcome(outcome: MountTestMoveOutcome) -> str:
    if outcome.error is not None:
        return f"Test move failed: {outcome.error}"
    parts = [
        f"{_CAMERA_LABELS.get(key, key)}: {_format_response(response)}"
        for key, response in outcome.responses.items()
    ]
    return " | ".join(parts)


_CAMERA_LABELS = {"left": "Main", "right": "Guide"}


def _format_response(response: AxisResponse) -> str:
    return (
        f"dx={response.dx_px:+.1f}px dy={response.dy_px:+.1f}px "
        f"({response.magnitude_px:.1f}px @ {response.angle_degrees:.0f}°)"
    )
