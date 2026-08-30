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
independently-connected copy of the same park/unpark state). The
direction buttons only enable while parked, for the same reason.

The mount actually has to be *unparked* to move at all — a real-hardware
check found OnStep's driver refuses `TELESCOPE_MOTION_NS`/`_WE` while
parked, a deliberate safety interlock (not a defect) — so
`MountTestMoveRunner` unparks before pulsing and re-parks after, every
run, respecting that interlock rather than routing around it; see its
own docstring.

Direction buttons fire immediately on click (N/S/E/W, like Park/Unpark
are direct actions) rather than "select a direction, then press a
separate confirm button" — a real user report (incident 9551627f) found
that select-then-confirm shape confusing on its own (a checked/exclusive
button "staying pressed" read as stuck, not as a live selection) and,
combined with the confirm button being silently disabled whenever the
mount wasn't parked with no explanation why, effectively unusable
("connect doesn't react to directions"). `_status_label` now always
explains *why* the direction buttons are disabled when they are (not
connected / mount unavailable / not parked / a move already in
progress), rather than just sitting there mute.

Same incident asked for a Stop control — real hardware motion with no
way to interrupt it once started is a real safety gap, not a nice-to-have
— see the "Stop" button, wired to `IndiMountPulseAdapter.abort()`
(`TELESCOPE_ABORT_MOTION`) via duck-typing (`getattr`, not a `MountPort`
Protocol method — that Protocol is the architecture doc's literal
contract, not something to extend unilaterally for one adapter's extra
capability). A no-op if the injected `mount` doesn't have `abort()` (e.g.
`NoMountAdapter`/`FakeMountAdapter` unless a test adds one).

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
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner

FrameGetter = Callable[[], np.ndarray | None]

_POLL_INTERVAL_MS = 250
_PULSE_MS = 500

#: Direction button order -- see IndiMountPulseAdapter's own docstring
#: for the (axis, direction) <-> compass-direction convention this
#: matches (AXIS1=RA/azimuth east/west, AXIS2=Dec/altitude north/south).
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
        self._pending_label: str | None = None
        self._last_responses: dict[str, AxisResponse] | None = None
        self._last_error: str | None = None

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")

        self._direction_buttons: list[QPushButton] = []
        direction_row = QHBoxLayout()
        direction_row.addWidget(QLabel(f"Test Move ({_PULSE_MS}ms, 20x)"))
        for label, axis, direction in _DIRECTIONS:
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, a=axis, d=direction, lbl=label: self._on_direction_clicked(
                    a, d, lbl
                )
            )
            self._direction_buttons.append(button)
            direction_row.addWidget(button)
        self._stop_button = QPushButton("Stop")
        self._stop_button.clicked.connect(self._on_stop)
        direction_row.addWidget(self._stop_button)
        direction_row.addStretch(1)

        self._result_label = QLabel("")
        self._result_label.setWordWrap(True)

        top_row = QHBoxLayout()
        top_row.addWidget(self._title_label)
        top_row.addWidget(self._connect_button)
        top_row.addWidget(self._status_label, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(direction_row)
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

    def _on_direction_clicked(self, axis: MountAxis, direction: AxisDirection, label: str) -> None:
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
        started = self._runner.submit(self._mount_park, self._mount, axis, direction, _PULSE_MS)
        if not started:
            return  # a test move is already running
        self._pending_before = before
        self._pending_direction = (axis, direction)
        self._pending_label = label
        self._result_label.setText(f"Testing ({label})…")
        self._update_buttons_enabled()

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
            self._finish_test_move(pulsed=outcome.pulsed, pulse_error=outcome.error)
        self._update_buttons_enabled()

    def _finish_test_move(self, *, pulsed: bool, pulse_error: str | None) -> None:
        before = self._pending_before
        direction = self._pending_direction
        self._pending_before = None
        self._pending_direction = None
        self._pending_label = None
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
            key: response_from_positions(axis, mount_direction, _PULSE_MS, before[key], after[key])
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
        busy = self._runner.is_busy
        can_test = self._connected and park_status.available and park_status.parked and not busy
        for button in self._direction_buttons:
            button.setEnabled(can_test)
        self._stop_button.setEnabled(self._connected and busy)

        # Explain *why* the direction buttons are disabled, rather than
        # leaving them silently unresponsive -- see module docstring's
        # incident note. Never stomps a "Testing…"/result/error message
        # that's still relevant (busy, or idle-and-eligible).
        if busy or can_test or not self._connected:
            return
        if not park_status.available:
            self._result_label.setText("Mount interface not available.")
        elif not park_status.parked:
            self._result_label.setText("Park the mount (see Mount panel above) to test a move.")

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
