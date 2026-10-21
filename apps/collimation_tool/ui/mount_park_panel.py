"""MountParkPanel — park/unpark-only control for the OnStep mount,
reached only through OnStepAdapter (see
`astrotool_core.onstep.OnStepMountParkAdapter`).

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

import threading
from collections.abc import Callable
from typing import Any

from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer
from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.session import ConnectionFailure, DeviceConnectionService
from astrotool_core.timing import SYSTEM_CLOCK, Clock
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from collimation_tool.application.mount_motion import MotionClaim, MountMotionExecutor
from collimation_tool.application.operation_lifecycle import (
    CompletionVerdict,
    StatusPollGuard,
    completion_verdict,
)

_POLL_INTERVAL_MS = 250
#: See FocuserPanel's identical constant for the reasoning — a safety net
#: in case a transition settles faster than this panel could ever catch
#: a Busy state, so the buttons don't stay disabled forever.
_TRANSITION_CONFIRMATION_TIMEOUT_S = 30.0


class MountParkPanel(QWidget):
    def __init__(
        self,
        mount: MountParkPort,
        *,
        title: str = "Mount",
        tracking_enforcer: TrackingEnforcer | None = None,
        clock: Clock | None = None,
        motion_executor: MountMotionExecutor | None = None,
    ) -> None:
        super().__init__()
        #: S6.8a (R3): a park/unpark is a mount motion like any other -- it claims the
        #: app-wide executor for as long as the command runs (one in-flight motion app-wide;
        #: while e.g. Mount Align moves, Park/Unpark are refused with the busy reason). None =
        #: no cross-workflow exclusion (a standalone panel).
        self._motion_executor = motion_executor
        #: Issue #53 (S3b): the time the confirmation-timeout safety net reads. The Qt
        #: poll timer stays the tick source; tests inject a `FakeClock`.
        self._clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        self._mount = mount
        #: Issue #44: terrestrial mode => tracking OFF, enforced on connect
        #: and after unpark, and surfaced in the status line.
        self._tracking_enforcer = tracking_enforcer
        #: #52 K01/K03 (S6.2): connect/disconnect lifecycle and failure reporting. START = the
        #: #44 tracking enforcement on connect (before the panel counts as connected, so a
        #: crash there can no longer leave the button and label inconsistent); WAIT = the first
        #: status read; CANCEL = tracking off on quit (the mount kept tracking after quit).
        self._connection = DeviceConnectionService(
            title,
            mount,
            start=lambda: self._enforce_tracking("connect"),
            wait=lambda: mount.status(),
            cancel=lambda: mount.stop_tracking(),
        )
        #: S6.3 (#52 L04): the periodic status poll never raises into Qt.
        self._poll_guard = StatusPollGuard(title)
        #: See module docstring's "one action at a time".
        self._action_in_flight = False
        self._pending_action: str | None = None  # "park" | "unpark" | None
        self._seen_busy_since_action = False
        self._action_issued_at: float | None = None
        self._action_worker: threading.Thread | None = None
        self._action_error: str | None = None
        #: S6.8c-R5: the executor claim of the park/unpark in flight -- the main window's
        #: mount Stop cancels it (`MotionClaim.cancelled`), and the outcome then reads
        #: "stopped by the user" instead of a failure (OnStepAdapter's `park()` has no cancel
        #: hook: after the ABORT it returns unconfirmed only once its own status wait gives up).
        self._action_claim: MotionClaim | None = None

        self._title_label = QLabel(f"<b>{title}</b>")
        self._connect_button = QPushButton("Connect")
        self._connect_button.setCheckable(True)
        self._connect_button.toggled.connect(self._on_toggle_connect)
        self._status_label = QLabel("Not connected.")

        self._park_button = QPushButton("Park")
        self._park_button.clicked.connect(self._on_park)
        self._unpark_button = QPushButton("Unpark")
        self._unpark_button.clicked.connect(self._on_unpark)
        #: OnStepAdapter refuses every motion until the OPERATOR has confirmed the mount is
        #: physically at its mechanical home -- an explicit human action, never automatic.
        #: Present only when the injected port offers it (`OnStepMountParkAdapter`).
        self._confirm_home_button = QPushButton("Confirm at home")
        self._confirm_home_button.setToolTip(
            "Tell OnStepAdapter the mount is physically at its home position. Required once "
            "before it will move the mount; only click when that is true."
        )
        self._confirm_home_button.clicked.connect(self._on_confirm_home)
        self._confirm_home_button.setVisible(hasattr(mount, "confirm_home"))

        action_row = QHBoxLayout()
        action_row.addWidget(self._park_button)
        action_row.addWidget(self._unpark_button)
        action_row.addWidget(self._confirm_home_button)
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

    @property
    def _connected(self) -> bool:
        return self._connection.connected

    def _on_toggle_connect(self, checked: bool) -> None:
        if checked:
            self._action_in_flight = False
            self._pending_action = None
            failure = self._connection.connect()
            if failure is not None:
                self._show_connection_failure(failure)
                return
            self._connect_button.setText("Disconnect")
            self._timer.start()
            self._poll_status()
        else:
            self._timer.stop()
            failures = self._connection.disconnect()
            self._action_in_flight = False
            self._pending_action = None
            self._connect_button.setText("Connect")
            self._status_label.setText(failures[-1].status_text if failures else "Not connected.")
        self._update_buttons_enabled()

    def _show_connection_failure(self, failure: ConnectionFailure) -> None:
        self._status_label.setText(failure.status_text)
        # blockSignals: resetting the button must not re-enter this handler with
        # checked=False, which would overwrite the reason with "Not connected.".
        self._connect_button.blockSignals(True)
        self._connect_button.setChecked(False)
        self._connect_button.blockSignals(False)
        self._update_buttons_enabled()

    def _enforce_tracking(self, context: str) -> None:
        if self._tracking_enforcer is not None:
            self._tracking_enforcer.enforce(context)

    def _policy_suffix(self, tracking: bool) -> str:
        """Issue #44: make the terrestrial requirement visible -- and an
        unexpected TRACKING ON impossible to miss."""
        enforcer = self._tracking_enforcer
        if enforcer is None or enforcer.mode is not OperatingMode.TERRESTRIAL:
            return ""
        if tracking:
            return " — TRACKING ON, terrestrial measurement blocked"
        if not enforcer.measurement_allowed():
            # S6.0c re-review P-b: e.g. the connect/unpark check found the mount busy -- the
            # denial is shown, never silently dropped.
            return f" — terrestrial measurement blocked: {enforcer.last_gate.reason}"
        return " — tracking OFF (required, terrestrial)"

    def _begin_action(self, action: str) -> None:
        claim: MotionClaim | None = None
        if self._motion_executor is not None:
            claimed = self._motion_executor.claim(f"the Mount panel's {action}")
            if isinstance(claimed, str):  # S6.8a: refused, never queued
                self._status_label.setText(f"{action.capitalize()} not started — {claimed}")
                return
            claim = claimed
        self._action_claim = claim
        # Disable synchronously, before issuing the action — see module
        # docstring. Qt delivers input on this one thread, so a button
        # already disabled here cannot receive a second click before
        # this handler returns.
        self._action_in_flight = True
        self._pending_action = action
        self._seen_busy_since_action = False
        self._action_issued_at = self._clock.monotonic()
        self._action_error = None
        self._update_buttons_enabled()
        run = self._mount.park if action == "park" else self._mount.unpark
        if getattr(self._mount, "long_running_actions", False):
            # Park slews for seconds to minutes (unpark is only a confirmed switch sequence,
            # but shares the same blocking adapter path): never on the GUI thread. The poll
            # loop watches the worker.
            self._action_worker = threading.Thread(
                target=self._run_action, args=(run, claim), daemon=True, name="mount-park-action"
            )
            self._action_worker.start()
        else:
            try:
                run()
            finally:
                self._release(claim)
            if self._stopped_by_user():
                self._status_label.setText(f"{action.capitalize()} stopped by the user.")

    def _run_action(self, run: Callable[[], None], claim: MotionClaim | None = None) -> None:
        try:
            run()
        except Exception as exc:  # noqa: BLE001 -- surfaced in the status line by the poll loop
            self._action_error = str(exc)
        finally:
            self._release(claim)

    def _stopped_by_user(self) -> bool:
        claim = self._action_claim
        return claim is not None and claim.cancelled

    def _release(self, claim: MotionClaim | None) -> None:
        if self._motion_executor is not None and claim is not None:
            self._motion_executor.release(claim)

    def _on_park(self) -> None:
        self._begin_action("park")

    def _on_unpark(self) -> None:
        self._begin_action("unpark")

    def _on_confirm_home(self) -> None:
        try:
            self._mount.confirm_home()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 -- shown to the operator, never swallowed silently
            self._status_label.setText(f"Confirm home failed — {exc}")
            return
        self._status_label.setText("Home position confirmed.")
        self._update_buttons_enabled()

    def _home_confirmed(self) -> bool | None:
        """None when the port has no such concept."""
        confirmed = getattr(self._mount, "home_confirmed", None)
        return None if confirmed is None else bool(confirmed)

    def _poll_status(self) -> None:
        """One QTimer tick. S6.3 (#52 L04): never raises into Qt -- a failing status read is
        shown, logged once per failure streak, and the transition safety net keeps running."""
        if not self._connected:
            return
        failure = self._poll_guard.run(self._poll_once)
        if failure is not None:
            self._status_label.setText(failure)
            if self._action_worker is None:
                self._observe_transition(busy=None)
            self._park_button.setEnabled(False)
            self._unpark_button.setEnabled(False)

    def _poll_once(self) -> None:
        status = self._mount.status()
        if not status.available:
            self._status_label.setText("Connected — no mount interface detected.")
        else:
            state = "Parked" if status.parked else "Unparked"
            tracking = "tracking" if status.tracking else "not tracking"
            home = {None: "", True: ", home confirmed", False: ", HOME NOT CONFIRMED"}[
                self._home_confirmed()
            ]
            self._status_label.setText(
                f"{state}, {tracking}{home}{self._policy_suffix(status.tracking)}"
            )
        worker = self._action_worker
        if worker is not None:
            if worker.is_alive():
                if self._stopped_by_user():
                    self._status_label.setText(
                        f"{self._pending_action or 'Action'} stopped by the user — waiting for "
                        "OnStepAdapter to end the command…"
                    )
                else:
                    self._status_label.setText(
                        f"{self._pending_action or 'Action'} in progress — the mount is moving…"
                    )
                self._update_buttons_enabled()
                return
            self._action_worker = None
            settled_action = self._pending_action
            self._action_in_flight = False
            self._pending_action = None
            if self._stopped_by_user():
                # Review: a Stop never hides what OnStepAdapter reported for the command.
                text = f"{(settled_action or 'action').capitalize()} stopped by the user"
                if self._action_error is not None:
                    text = f"{text} — OnStepAdapter: {self._action_error}"
                self._status_label.setText(f"{text}.")
                self._update_buttons_enabled()
                return
            if self._action_error is not None:
                self._status_label.setText(f"{settled_action} failed — {self._action_error}")
                self._update_buttons_enabled()
                return
            if settled_action == "unpark":
                self._enforce_tracking("unpark")
        if self._action_in_flight:
            # "Busy" here means "not yet settled at the target state" --
            # still parked right after an unpark request, or vice versa.
            target_parked = self._pending_action == "park"
            self._observe_transition(busy=status.available and status.parked != target_parked)
        self._update_buttons_enabled()

    def _observe_transition(self, *, busy: bool | None) -> None:
        """S6.3 (#52 L01): `completion_verdict`, the one rule of the one-action-at-a-time
        panels. This panel still had the pre-3fc09ae shape -- the safety-net timeout sat in an
        `elif` reachable only while NOT busy, so a park/unpark that never reached its target
        kept Park/Unpark disabled forever (ports without `long_running_actions`)."""
        if not self._action_in_flight:
            return
        self._seen_busy_since_action, verdict = completion_verdict(
            busy=busy,
            seen_busy=self._seen_busy_since_action,
            issued_at=self._action_issued_at,
            now=self._clock.monotonic(),
            timeout_s=_TRANSITION_CONFIRMATION_TIMEOUT_S,
        )
        if verdict is CompletionVerdict.PENDING:
            return
        settled_action = self._pending_action
        self._action_in_flight = False
        self._pending_action = None
        if verdict is CompletionVerdict.CONFIRMED and settled_action == "unpark":
            self._enforce_tracking("unpark")

    def _update_buttons_enabled(self) -> None:
        self._confirm_home_button.setEnabled(self._connected and self._mount.is_available)
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
            "last_connect_error": self._connection.last_connect_error,
            "connection": self._connection.diagnostic_context(),
        }

    def stop(self) -> None:
        """Stop polling and disconnect. Safe to call whether or not connected.

        Deactivates tracking first -- real report: the mount kept tracking
        after quitting the app. Deliberately doesn't park (see
        MountParkPort.stop_tracking's own docstring for why that stays a
        separate, explicit user action, not something quitting does on its
        own initiative)."""
        self._timer.stop()
        # #52 K03: tracking off that is not confirmed (OnStepAdapter#17) never skips the release
        # of the shared OnStep connection, and nothing raises out of quit.
        self._connection.disconnect(cancel_motion=True)
