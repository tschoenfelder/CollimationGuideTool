"""The "Keep the star centred" control: reads fresh Main frames off the GUI thread, asks
`StarCenterer` what to do and hands moves to Mount Align (`move_main`)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from astrotool_core.acquisition.stable_frame_acquisition import StableFrameWaiter
from astrotool_core.focus.disc_locator import locate_main_disc
from astrotool_core.timing import SYSTEM_CLOCK, Clock
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from collimation_tool.application.operation_lifecycle import OperationLifecycle
from collimation_tool.application.star_centering import (
    BELOW_SMALLEST_MOVE,
    CENTERED_FRACTION,
    MOVE_GAIN,
    CenteringAction,
    StarCenterer,
)

_POLL_INTERVAL_MS = 300
#: A fresh frame may be this many camera cycles (plus the margin) away.
_WAIT_CYCLES = 3.0
_WAIT_MARGIN_S = 5.0
#: An offset this close (share of the frame height) that the mount cannot correct because the
#: move is below its smallest one counts as centred.
_NEAR_FRACTION = 3 * CENTERED_FRACTION


@dataclass(frozen=True)
class _Reading:
    position: tuple[float, float] | None
    frame_shape: tuple[int, int]
    error: str | None = None


class StarCenteringPanel(QWidget):
    def __init__(
        self,
        *,
        wait_for_frame_after: StableFrameWaiter,
        cycle_s: Callable[[], float | None],
        #: Why nothing may move now (shown as is), else None.
        blocked: Callable[[], str | None],
        #: True while a mount move is running.
        moving: Callable[[], bool],
        #: Start a move shifting the Main image by (dx, dy) px; the refusal text, else None.
        move_main: Callable[[float, float], str | None],
        clock: Clock | None = None,
    ) -> None:
        super().__init__()
        self._wait = wait_for_frame_after
        self._cycle_s = cycle_s
        self._blocked = blocked
        self._moving = moving
        self._move_main = move_main
        self._clock = clock or SYSTEM_CLOCK
        self._centerer = StarCenterer()
        self._lifecycle: OperationLifecycle[_Reading] = OperationLifecycle("star-centering")
        self._latest: _Reading | None = None
        #: Only frames exposed after this instant count (moved past every move and pause).
        self._fresh_after = 0.0

        self._checkbox = QCheckBox("Keep the star centred in Main (moves the mount)")
        self._checkbox.toggled.connect(self._on_toggled)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._checkbox)
        layout.addWidget(self._status)

        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_INTERVAL_MS)
        self._timer.timeout.connect(self.poll)

    @property
    def active(self) -> bool:
        return self._checkbox.isChecked()

    def set_active(self, active: bool) -> None:
        self._checkbox.setChecked(active)

    @property
    def status_text(self) -> str:
        return self._status.text()

    def stop(self) -> None:
        self.set_active(False)

    def fail(self, reason: str) -> None:
        """A centring move did not complete: switch off and say why."""
        if self.active:
            self._give_up(f"the move failed: {reason}")

    def _on_toggled(self, checked: bool) -> None:
        self._centerer.reset()
        self._latest = None
        self._fresh_after = self._clock.monotonic()
        if checked:
            self._status.setText("Looking for the star…")
            self._timer.start()
        else:
            self._timer.stop()
            self._lifecycle.cancel()
            self._status.setText("")

    def poll(self) -> None:
        if not self.active:
            return
        reason = self._blocked()
        if reason is not None or self._moving():
            # Whatever was read before or during this pause is stale.
            self._latest = None
            self._fresh_after = self._clock.monotonic()
            self._status.setText(f"Centring waits: {reason}" if reason else "Moving the mount…")
            return
        with self._lifecycle.lock:
            reading, self._latest = self._latest, None
        if reading is not None:
            self._act_on(reading)
            return
        if not self._lifecycle.is_busy:
            self._start_reading()

    def _start_reading(self) -> None:
        after = max(self._fresh_after, self._clock.monotonic())
        timeout_s = _WAIT_CYCLES * (self._cycle_s() or 0.0) + _WAIT_MARGIN_S

        def work() -> _Reading:
            result = self._wait(after, timeout_s)
            if not result.ok or result.frame is None:
                return _Reading(None, (0, 0), "no fresh Main frame -- is Main streaming?")
            pixels = result.frame.pixels
            disc = locate_main_disc(pixels)
            position = None if disc is None else (disc.x, disc.y)
            return _Reading(position, (pixels.shape[0], pixels.shape[1]))

        def publish(reading: _Reading) -> None:
            # A reading that started before a pause or a move is dropped.
            if after >= self._fresh_after:
                self._latest = reading

        self._lifecycle.start(
            work,
            on_error=lambda exc: _Reading(None, (0, 0), f"{type(exc).__name__}: {exc}"),
            publish=publish,
        )

    def _act_on(self, reading: _Reading) -> None:
        if reading.error is not None:
            self._status.setText(f"Centring waits: {reading.error}")
            return
        decision = self._centerer.read(reading.position, reading.frame_shape)
        self._status.setText(decision.text[:1].upper() + decision.text[1:])
        if decision.action is CenteringAction.MOVE:
            refusal = self._move_main(decision.dx_px, decision.dy_px)
            off_px = math.hypot(decision.dx_px, decision.dy_px) / MOVE_GAIN
            near_px = _NEAR_FRACTION * reading.frame_shape[0]
            if refusal == BELOW_SMALLEST_MOVE and off_px <= near_px:
                self._status.setText("Star centred (closer than the mount's smallest move)")
                return
            if refusal is not None:
                self._give_up(f"the move was refused: {refusal}")
                return
            self._centerer.moved()
            self._fresh_after = self._clock.monotonic()
        elif decision.action is CenteringAction.GIVE_UP:
            self._give_up(decision.text)

    def _give_up(self, reason: str) -> None:
        self._checkbox.setChecked(False)
        self._status.setText(f"Centring stopped: {reason}")
