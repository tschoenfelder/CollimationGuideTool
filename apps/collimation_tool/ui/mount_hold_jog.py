"""Hold-to-move on the RA/Dec pad (Torsten, 2026-11-08): a button held longer than
`HOLD_THRESHOLD_MS` moves its axis for as long as it stays pressed; a quick click stays the
measured nudge it always was.

The motion is OnStepAdapter's jog (`JogPort`, AGENTS.md "Mount movement boundary": no raw INDI
manual-motion switches here). While the button is down this renews the jog's deadman lease
every `RENEW_INTERVAL_MS`; on release, or as soon as the button is no longer down (disabled,
the window lost the mouse), it releases the jog, and OnStepAdapter switches the motion off and
confirms the stop. If this GUI thread stalls, the lease is not renewed and OnStepAdapter stops
the axis on its own. A jog runs under one `MotionClaim` of the app's motion executor (one mount
motion app-wide; its Stop ends the jog at once) and passes the executor's tracking rule like
any other axis move.

The DEC-first rule (OnStepAdapter): after connecting and at HOME, RA is undefined until Dec
has moved, so the RA buttons are disabled until then (`MountTestMovePanel`) and an RA jog
would be refused anyway.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from astrotool_core.mount import AxisDirection, JogPort, MountAxis, MountParkPort
from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QPushButton

from collimation_tool.application.mount_align_messages import AXIS_LABELS
from collimation_tool.application.mount_motion import MotionClaim, MountMotionExecutor

#: How long a press must last before it becomes a hold (a shorter one is a click = nudge).
HOLD_THRESHOLD_MS = 350
#: How often the deadman lease is renewed while held (well inside the adapter's 0.5 s lease).
RENEW_INTERVAL_MS = 150
#: The claim owner name shown in another workflow's busy refusal.
JOG_OWNER = "Mount Align jog"

_ENDINGS = {
    "released": "released",
    "lease_expired": "stopped: the hold was not renewed in time",
    "max_duration": "stopped: the longest single hold was reached",
    "limit": "stopped at the movement limit",
}


def _label(press: _Press) -> str:
    sign = "+" if press.direction is AxisDirection.POSITIVE else "-"
    return f"{AXIS_LABELS[press.axis]} {sign}"


def supports_jog(mount: object) -> bool:
    return bool(getattr(mount, "supports_jog", False))


@dataclass
class _Press:
    camera_key: str
    axis: MountAxis
    direction: AxisDirection
    button: QPushButton
    #: Set once the hold started a jog (None: still a possible click, or the jog was refused).
    claim: MotionClaim | None = None
    refused: bool = False
    #: Ended by the panel's Stop (the executor already sent the emergency stop).
    stopped: bool = False


class HoldToJog(QObject):
    def __init__(
        self,
        *,
        mount: object,
        mount_park: MountParkPort | None,
        executor: MountMotionExecutor,
        on_click: Callable[[str, MountAxis, AxisDirection], None],
        show: Callable[[str], None],
        changed: Callable[[], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._mount = mount
        self._mount_park = mount_park
        self._executor = executor
        self._on_click = on_click
        self._show = show
        self._changed = changed
        self._press: _Press | None = None
        #: The jog released but not yet confirmed stopped (its claim is still held).
        self._ending: _Press | None = None
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(HOLD_THRESHOLD_MS)
        self._hold_timer.timeout.connect(self._on_hold)
        self._renew_timer = QTimer(self)
        self._renew_timer.setInterval(RENEW_INTERVAL_MS)
        self._renew_timer.timeout.connect(self._on_tick)

    @property
    def active(self) -> bool:
        """A jog is moving or still confirming its stop."""
        return (self._press is not None and self._press.claim is not None) or (
            self._ending is not None
        )

    @property
    def held_button(self) -> QPushButton | None:
        press = self._press
        return press.button if press is not None and press.claim is not None else None

    # ---- button signals ------------------------------------------------------------------
    def press(
        self, camera_key: str, axis: MountAxis, direction: AxisDirection, button: QPushButton
    ) -> None:
        if self._press is not None or self._ending is not None:
            return
        self._press = _Press(camera_key, axis, direction, button)
        if supports_jog(self._mount):
            self._hold_timer.start()

    def release(self) -> None:
        press, self._press = self._press, None
        self._hold_timer.stop()
        if press is None:
            return
        if press.claim is not None:
            self._end(press)
        elif not press.refused:
            self._on_click(press.camera_key, press.axis, press.direction)

    def abort(self) -> None:
        """Stop/quit: the executor's Stop already stopped the mount; end the bookkeeping."""
        press, self._press = self._press, None
        self._hold_timer.stop()
        if press is not None and press.claim is not None:
            press.stopped = True
            self._end(press)

    # ---- the jog -------------------------------------------------------------------------
    def _on_hold(self) -> None:
        press = self._press
        if press is None or not press.button.isDown():
            return
        label = _label(press)
        claim = self._executor.claim(JOG_OWNER, self._mount)
        if isinstance(claim, str):
            press.refused = True
            self._show(f"Move failed: {claim}")
            return
        status = self._mount_park.status() if self._mount_park is not None else None
        tracking = status.tracking if status is not None and status.available else None
        if self._mount_park is not None:
            gate = self._executor.axis_move_gate(tracking, "jog")
            if not gate.allowed:
                self._executor.release(claim)
                press.refused = True
                self._show(f"Move failed: {gate.reason}")
                return
        result = cast(JogPort, self._mount).start_jog(press.axis, press.direction)
        if not result.accepted:
            self._executor.release(claim)
            press.refused = True
            self._show(f"Move failed: {result.message}")
            return
        press.claim = claim
        self._show(f"Moving {label} while the button is held…")
        self._renew_timer.start()
        self._changed()

    def _on_tick(self) -> None:
        press = self._press
        jog = cast(JogPort, self._mount)
        if press is not None and press.claim is not None:
            if not press.button.isDown():  # disabled or lost the mouse: never keep moving
                self._press = None
                self._end(press)
            elif not jog.renew_jog():  # ended by itself (deadman, limit, Stop)
                self._press = None
                self._ending = press
            return
        self._poll_ending()

    def _end(self, press: _Press) -> None:
        cast(JogPort, self._mount).release_jog()
        self._ending = press
        self._renew_timer.start()
        self._poll_ending()

    def _poll_ending(self) -> None:
        press = self._ending
        if press is None:
            self._renew_timer.stop()
            return
        outcome = cast(JogPort, self._mount).jog_outcome()
        if outcome is None:
            return
        self._ending = None
        self._renew_timer.stop()
        if press.claim is not None:
            self._executor.release(press.claim)
        label = _label(press)
        if press.stopped:
            self._show(f"Stopped {label}: stopped by the user")
        elif outcome.ok:
            ending = _ENDINGS.get(outcome.ended_by, outcome.ended_by or "done")
            self._show(f"Moved {label} ({ending})")
        else:
            self._show(f"Move failed: {outcome.message}")
        self._changed()
