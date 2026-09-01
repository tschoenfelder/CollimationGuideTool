"""onstep_adapter_uat.py — small standalone PySide6 UI for manual UAT of
the three OnStep INDI adapters reworked this session:
`IndiMountParkAdapter`, `IndiMountPulseAdapter`, `IndiFocuserAdapter`
(packages/astrotool_core/mount, .../focus).

Not part of the shipped app (`apps/collimation_tool`) -- deliberately
thin, no cameras, no diagnostics service, no config file. Each section
below wires directly to the real adapter class, unmodified, the same
way `apps/collimation_tool/main.py`'s `_default_mount()`/
`_default_pulse_mount()`/`_default_focuser()` do (zero-arg defaults:
host="localhost", port=7624, device_name="LX200 OnStep") -- point
`--host`/`--port` elsewhere if indiserver isn't local.

What this exists to let a human directly exercise, with immediate
visible pass/fail feedback, against real hardware:

- **Mount Park** (`IndiMountParkAdapter`): Park/Unpark/Stop Tracking,
  live `parked`/`tracking` status. Real fix under test (commit
  `5b84a64`): `status().parked` now treats a `Busy` TELESCOPE_PARK
  vector as still-parked rather than trusting an optimistic mid-
  transition echo -- watch the status label through a park/unpark
  cycle to confirm it never flips early.
- **Mount Pulse** (`IndiMountPulseAdapter`): one `pulse_axis()` call
  per click, with the exact axis/direction/duration/rate this UI sends
  and the driver's real accepted/rejected response, both shown
  directly (see result label). Real fix under test (commits `bb95cd2`,
  `737188f`): a pulse while parked now reports `accepted=False`
  ("mount rejected the motion command -- still parked?") near-
  instantly instead of silently sleeping out the full duration and
  claiming success; turning motion back off is now also confirmed,
  falling back to `abort()` if it never lands.
- **Focuser** (`IndiFocuserAdapter`): Move In/Out (relative), Move
  Absolute (with accepted/rejected shown), Stop, live position/moving
  status. Real fix under test (commit `714dc34`): `move_absolute()`
  now reports `accepted=False` on a driver-rejected (`IPS_ALERT`) move
  instead of always claiming success.

Each section polls its own `status()` on a 250ms QTimer, mirroring
`apps/collimation_tool/ui/*_panel.py`'s own established pattern --
this script intentionally doesn't reimplement any of that UI, just
wires the same three adapter classes directly with the bare minimum
around them to click a button and read a result.

Usage:
    python scripts/onstep_adapter_uat.py [--host HOST] [--port PORT]
"""

from __future__ import annotations

import argparse
import logging

from astrotool_core.focus.indi_focuser_adapter import IndiFocuserAdapter
from astrotool_core.mount.indi_mount_park_adapter import IndiMountParkAdapter
from astrotool_core.mount.indi_mount_pulse_adapter import IndiMountPulseAdapter
from astrotool_core.mount.port import AxisDirection, MountAxis
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

_log = logging.getLogger("onstep_adapter_uat")

_POLL_INTERVAL_MS = 250


class _MountParkSection(QGroupBox):
    def __init__(self, mount: IndiMountParkAdapter) -> None:
        super().__init__("Mount Park (IndiMountParkAdapter)")
        self._mount = mount
        self._connected = False

        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")
        self._park_button = QPushButton("Park")
        self._park_button.clicked.connect(self._mount.park)
        self._unpark_button = QPushButton("Unpark")
        self._unpark_button.clicked.connect(self._mount.unpark)
        self._stop_tracking_button = QPushButton("Stop Tracking")
        self._stop_tracking_button.clicked.connect(self._mount.stop_tracking)

        row = QHBoxLayout()
        row.addWidget(self._connect_button)
        row.addWidget(self._park_button)
        row.addWidget(self._unpark_button)
        row.addWidget(self._stop_tracking_button)
        row.addStretch(1)
        layout = QVBoxLayout()
        layout.addLayout(row)
        layout.addWidget(self._status_label)
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
            self._connect_button.setText("Disconnect")
            self._timer.start()
        else:
            self._timer.stop()
            self._mount.disconnect()
            self._connected = False
            self._connect_button.setText("Connect")
            self._status_label.setText("Not connected.")
        self._update_buttons_enabled()

    def _poll_status(self) -> None:
        status = self._mount.status()
        self._status_label.setText(
            f"available={status.available}  parked={status.parked}  tracking={status.tracking}"
        )

    def _update_buttons_enabled(self) -> None:
        for button in (self._park_button, self._unpark_button, self._stop_tracking_button):
            button.setEnabled(self._connected)

    def stop(self) -> None:
        self._timer.stop()
        if self._connected:
            self._mount.disconnect()
            self._connected = False


class _MountPulseSection(QGroupBox):
    def __init__(self, mount: IndiMountPulseAdapter) -> None:
        super().__init__("Mount Pulse (IndiMountPulseAdapter)")
        self._mount = mount
        self._connected = False

        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)

        self._axis_combo = QComboBox()
        self._axis_combo.addItem("AXIS1 (RA/az)", MountAxis.AXIS1)
        self._axis_combo.addItem("AXIS2 (Dec/alt)", MountAxis.AXIS2)
        self._direction_combo = QComboBox()
        self._direction_combo.addItem("POSITIVE", AxisDirection.POSITIVE)
        self._direction_combo.addItem("NEGATIVE", AxisDirection.NEGATIVE)
        self._duration_spin = QSpinBox()
        self._duration_spin.setRange(1, 9999)
        self._duration_spin.setValue(1000)
        self._duration_spin.setSuffix(" ms")
        self._rate_edit = QLineEdit("7")
        self._rate_edit.setFixedWidth(30)
        self._pulse_button = QPushButton("Pulse")
        self._pulse_button.clicked.connect(self._on_pulse_clicked)
        self._abort_button = QPushButton("Abort")
        self._abort_button.clicked.connect(self._mount.abort)

        row = QHBoxLayout()
        row.addWidget(self._connect_button)
        row.addWidget(QLabel("Axis"))
        row.addWidget(self._axis_combo)
        row.addWidget(QLabel("Direction"))
        row.addWidget(self._direction_combo)
        row.addWidget(QLabel("Duration"))
        row.addWidget(self._duration_spin)
        row.addWidget(QLabel("Rate"))
        row.addWidget(self._rate_edit)
        row.addWidget(self._pulse_button)
        row.addWidget(self._abort_button)
        row.addStretch(1)
        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)
        layout = QVBoxLayout()
        layout.addLayout(row)
        layout.addWidget(self._result_label)
        self.setLayout(layout)
        self._update_buttons_enabled()

    def _on_toggle_connect(self, checked: bool) -> None:
        if checked:
            try:
                self._mount.connect()
            except ConnectionError as exc:
                self._result_label.setText(f"Connect failed — {exc}")
                self._connect_button.blockSignals(True)
                self._connect_button.setChecked(False)
                self._connect_button.blockSignals(False)
                self._update_buttons_enabled()
                return
            self._connected = True
            self._connect_button.setText("Disconnect")
        else:
            self._mount.disconnect()
            self._connected = False
            self._connect_button.setText("Connect")
        self._update_buttons_enabled()

    def _on_pulse_clicked(self) -> None:
        axis = self._axis_combo.currentData()
        direction = self._direction_combo.currentData()
        duration_ms = self._duration_spin.value()
        rate_preset = self._rate_edit.text().strip() or None
        self._result_label.setText("Pulsing…")
        self._pulse_button.setEnabled(False)
        QApplication.processEvents()  # let "Pulsing…" actually paint before the blocking call
        try:
            result = self._mount.pulse_axis(
                axis, direction, duration_ms, rate_preset=rate_preset
            )
        finally:
            self._pulse_button.setEnabled(True)
        self._result_label.setText(
            f"accepted={result.accepted}"
            + (f"  message={result.message!r}" if result.message else "")
        )

    def _update_buttons_enabled(self) -> None:
        for widget in (self._pulse_button, self._abort_button):
            widget.setEnabled(self._connected)

    def stop(self) -> None:
        if self._connected:
            self._mount.disconnect()
            self._connected = False


class _FocuserSection(QGroupBox):
    def __init__(self, focuser: IndiFocuserAdapter) -> None:
        super().__init__("Focuser (IndiFocuserAdapter)")
        self._focuser = focuser
        self._connected = False

        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")

        self._step_spin = QSpinBox()
        self._step_spin.setRange(1, 100000)
        self._step_spin.setValue(100)
        self._in_button = QPushButton("In")
        self._in_button.clicked.connect(lambda: self._focuser.move(-self._step_spin.value()))
        self._out_button = QPushButton("Out")
        self._out_button.clicked.connect(lambda: self._focuser.move(self._step_spin.value()))

        self._target_spin = QSpinBox()
        self._target_spin.setRange(0, 1_000_000)
        self._move_absolute_button = QPushButton("Move Absolute")
        self._move_absolute_button.clicked.connect(self._on_move_absolute_clicked)

        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self._focuser.stop)

        move_row = QHBoxLayout()
        move_row.addWidget(self._connect_button)
        move_row.addWidget(QLabel("Steps"))
        move_row.addWidget(self._step_spin)
        move_row.addWidget(self._in_button)
        move_row.addWidget(self._out_button)
        move_row.addWidget(self._stop_button)
        move_row.addStretch(1)

        absolute_row = QHBoxLayout()
        absolute_row.addWidget(QLabel("Target position"))
        absolute_row.addWidget(self._target_spin)
        absolute_row.addWidget(self._move_absolute_button)
        absolute_row.addStretch(1)

        self._result_label = QLabel("")
        layout = QVBoxLayout()
        layout.addLayout(move_row)
        layout.addLayout(absolute_row)
        layout.addWidget(self._status_label)
        layout.addWidget(self._result_label)
        self.setLayout(layout)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_status)
        self._update_buttons_enabled()

    def _on_toggle_connect(self, checked: bool) -> None:
        if checked:
            try:
                self._focuser.connect()
            except ConnectionError as exc:
                self._status_label.setText(f"Connect failed — {exc}")
                self._connect_button.blockSignals(True)
                self._connect_button.setChecked(False)
                self._connect_button.blockSignals(False)
                self._update_buttons_enabled()
                return
            self._connected = True
            self._connect_button.setText("Disconnect")
            self._target_spin.setValue(self._focuser.get_position())
            self._timer.start()
        else:
            self._timer.stop()
            self._focuser.disconnect()
            self._connected = False
            self._connect_button.setText("Connect")
            self._status_label.setText("Not connected.")
        self._update_buttons_enabled()

    def _on_move_absolute_clicked(self) -> None:
        result = self._focuser.move_absolute(self._target_spin.value())
        self._result_label.setText(
            f"accepted={result.accepted}  target={result.target_position}  "
            f"start={result.start_position}"
        )

    def _poll_status(self) -> None:
        status = self._focuser.status()
        self._status_label.setText(
            f"available={status.available}  position={status.position}  "
            f"max={status.max_position}  moving={status.moving}"
        )

    def _update_buttons_enabled(self) -> None:
        for widget in (
            self._in_button,
            self._out_button,
            self._move_absolute_button,
            self._stop_button,
        ):
            widget.setEnabled(self._connected)

    def stop(self) -> None:
        self._timer.stop()
        if self._connected:
            self._focuser.disconnect()
            self._connected = False


class _UatWindow(QMainWindow):
    def __init__(self, host: str, port: int) -> None:
        super().__init__()
        self.setWindowTitle(f"OnStep INDI Adapter UAT — {host}:{port}")
        self._mount_park_section = _MountParkSection(IndiMountParkAdapter(host, port))
        self._mount_pulse_section = _MountPulseSection(IndiMountPulseAdapter(host, port))
        self._focuser_section = _FocuserSection(IndiFocuserAdapter(host, port))

        central = QWidget()
        layout = QVBoxLayout()
        layout.addWidget(self._mount_park_section)
        layout.addWidget(self._mount_pulse_section)
        layout.addWidget(self._focuser_section)
        layout.addStretch(1)
        central.setLayout(layout)
        self.setCentralWidget(central)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        self._mount_park_section.stop()
        self._mount_pulse_section.stop()
        self._focuser_section.stop()
        super().closeEvent(event)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=7624)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )

    app = QApplication([])
    window = _UatWindow(args.host, args.port)
    window.resize(700, 400)
    window.show()
    app.exec()


if __name__ == "__main__":
    main()
