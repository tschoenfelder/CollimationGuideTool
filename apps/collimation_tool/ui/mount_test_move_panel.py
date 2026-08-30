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

Also takes the *same* `MountParkPort` object `MountParkPanel` uses
(`mount_park` constructor param — deliberately the shared instance, not
this panel's own connection: `MountTestMoveRunner` drives it directly,
and it's simplest for that to be the one connection already managed by
`MountParkPanel`'s own Connect button rather than a second,
independently-connected copy of the same park/unpark state). The gate
("Test Move" only enabled while parked) reads `mount_park.status()`
directly for the same reason.

The mount actually has to be *unparked* to move at all — a real-hardware
check found OnStep's driver refuses `TELESCOPE_MOTION_NS`/`_WE` while
parked, a deliberate safety interlock (not a defect) — so
`MountTestMoveRunner` unparks before pulsing and re-parks after, every
run, respecting that interlock rather than routing around it; see its
own docstring.

Frame capture happens *here*, on the Qt main thread, both before
submitting the pulse and again once the runner reports it finished —
deliberately never on the runner's background thread (a real crash was
traced to exactly that: calling `CameraPanel.latest_mono_frame()`
concurrently from a background thread while the same panel's own poll
timer delivers frames on the main thread — see `MountTestMoveRunner`'s
docstring). Detection (`detect_sources`) is fast enough for a single
frame that doing it twice inline on the UI thread doesn't freeze
anything, unlike FOV registration's multi-candidate search.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
from astrotool_core.mount.axis_calibration import AxisResponse, response_from_positions
from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.mount.port import AxisDirection, MountAxis, MountPort
from astrotool_core.target.detector import detect_sources
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner

FrameGetter = Callable[[], np.ndarray | None]

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

_CAMERA_LABELS = {"left": "Main", "right": "Guide"}


class MountTestMovePanel(QWidget):
    def __init__(
        self,
        mount: MountPort,
        *,
        mount_park: MountParkPort,
        get_left_frame: FrameGetter,
        get_right_frame: FrameGetter,
        title: str = "Test Move",
        runner: MountTestMoveRunner | None = None,
    ) -> None:
        super().__init__()
        self._mount = mount
        self._mount_park = mount_park
        self._get_left_frame = get_left_frame
        self._get_right_frame = get_right_frame
        self._runner = runner if runner is not None else MountTestMoveRunner()
        self._connected = False
        self._pending_before: dict[str, tuple[float, float]] | None = None
        self._pending_direction: tuple[MountAxis, AxisDirection] | None = None
        self._last_responses: dict[str, AxisResponse] | None = None
        self._last_error: str | None = None

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
        # Captured here, on the Qt main thread -- see module docstring
        # for why this must never happen on the runner's background one.
        before_raw = {
            "left": _measure_brightest_source(self._get_left_frame()),
            "right": _measure_brightest_source(self._get_right_frame()),
        }
        missing = [key for key, position in before_raw.items() if position is None]
        if missing:
            # Don't bother moving the real mount if there's already
            # nothing to measure a displacement against.
            self._last_responses = None
            self._last_error = f"no star detected in: {', '.join(missing)}"
            self._result_label.setText(f"Test move failed: {self._last_error}")
            return
        before: dict[str, tuple[float, float]] = before_raw  # type: ignore[assignment]
        direction = self._selected_direction()
        started = self._runner.submit(self._mount_park, self._mount, *direction, _PULSE_MS)
        if not started:
            return  # a test move is already running
        self._pending_before = before
        self._pending_direction = direction
        self._result_label.setText("Testing…")
        self._update_buttons_enabled()

    def _poll(self) -> None:
        if not self._connected:
            return
        outcome = self._runner.take_latest()
        if outcome is not None:
            self._finish_test_move(pulsed=outcome.pulsed, pulse_error=outcome.error)
        self._update_buttons_enabled()

    def _finish_test_move(self, *, pulsed: bool, pulse_error: str | None) -> None:
        before = self._pending_before
        direction = self._pending_direction
        self._pending_before = None
        self._pending_direction = None
        if before is None or direction is None:
            return  # defensive -- take_latest() without a matching submit()
        if not pulsed:
            self._last_responses = None
            self._last_error = pulse_error or "pulse failed"
            self._result_label.setText(f"Test move failed: {self._last_error}")
            return

        axis, mount_direction = direction
        after_raw = {
            "left": _measure_brightest_source(self._get_left_frame()),
            "right": _measure_brightest_source(self._get_right_frame()),
        }
        missing = [key for key, position in after_raw.items() if position is None]
        if missing:
            self._last_responses = None
            self._last_error = f"no star detected (after the move) in: {', '.join(missing)}"
            self._result_label.setText(f"Test move failed: {self._last_error}")
            return
        after: dict[str, tuple[float, float]] = after_raw  # type: ignore[assignment]

        responses = {
            key: response_from_positions(
                axis, mount_direction, _PULSE_MS, before[key], after[key]
            )
            for key in ("left", "right")
        }
        self._last_responses = responses
        self._last_error = None
        parts = [
            f"{_CAMERA_LABELS[key]}: {_format_response(response)}"
            for key, response in responses.items()
        ]
        self._result_label.setText(" | ".join(parts))

    def _update_buttons_enabled(self) -> None:
        park_status = self._mount_park.status()
        can_test = (
            self._connected
            and park_status.available
            and park_status.parked
            and not self._runner.is_busy
        )
        self._test_move_button.setEnabled(can_test)

    def diagnostic_context(self) -> dict[str, Any]:
        if self._last_error is not None:
            return {"last_result": {"error": self._last_error}}
        if self._last_responses is None:
            return {"last_result": None}
        return {
            "last_result": {
                key: {
                    "dx_px": response.dx_px,
                    "dy_px": response.dy_px,
                    "magnitude_px": response.magnitude_px,
                    "angle_degrees": response.angle_degrees,
                }
                for key, response in self._last_responses.items()
            }
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected."""
        self._timer.stop()
        if self._connected:
            self._mount.disconnect()
            self._connected = False


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
