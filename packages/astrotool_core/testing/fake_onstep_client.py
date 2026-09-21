"""FakeOnStepClient — in-memory stand-in for `onstep_adapter.OnStepClient`.

The `astrotool_core.onstep` adapters are thin shims over OnStepAdapter, so the
right test double is a fake of OnStepAdapter's own public surface (its
`client.mount` / `client.focuser`), not of INDI or a serial line. It records
every call and models only the behaviours the shims depend on: a parked mount
refusing motion, `manual` motion requiring tracking off, `center` motion
refusing at home, a motion lock, and cancellation via `cancel_check`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from onstep_adapter import (
    AxisMotionResult,
    FocuserMoveResult,
    FocuserStatus,
    OnStepSafetyError,
    SafetySeverity,
    SafetyViolation,
)
from onstep_adapter.ports.mount import MountState


def _blocked(reason: str, command: str) -> OnStepSafetyError:
    return OnStepSafetyError(
        SafetyViolation(reason=reason, command=command, severity=SafetySeverity.BLOCKED)
    )


@dataclass
class MotionCall:
    axis: str
    direction: str
    duration_ms: int
    mode: str
    rate_preset: int | None


class FakeOnStepMount:
    def __init__(self) -> None:
        self.state = MountState.PARKED
        self.at_home = True
        self.connected = False
        self.motion_calls: list[MotionCall] = []
        self.stop_calls = 0
        self.unpark_rejected = False
        self.tracking_sticks_on = False
        #: Real time the fake "moves" for; 0 keeps tests instant.
        self.simulate_motion_s = 0.0
        self.calls: list[str] = []

    def connect(self) -> bool:
        self.connected = True
        return True

    def disconnect(self) -> None:
        self.connected = False

    def get_state(self) -> MountState:
        return self.state

    def is_slewing(self) -> bool:
        return bool(self.state == MountState.SLEWING)

    def park(self) -> bool:
        self.calls.append("park")
        self.state = MountState.PARKED
        return True

    def unpark(self) -> bool:
        self.calls.append("unpark")
        if self.unpark_rejected:
            return False
        self.state = MountState.UNPARKED
        return True

    def recovery_unpark_stop_tracking(self) -> dict[str, object]:
        self.calls.append("recovery_unpark_stop_tracking")
        if self.unpark_rejected:
            return {"ok": False}
        self.state = MountState.UNPARKED
        return {"ok": True}

    def enable_tracking(self) -> bool:
        self.calls.append("enable_tracking")
        if self.state == MountState.PARKED:
            return False
        self.state = MountState.TRACKING
        self.at_home = False
        return True

    def disable_tracking_verified(self, **_: object) -> dict[str, object]:
        self.calls.append("disable_tracking_verified")
        if self.tracking_sticks_on:
            return {"ok": False, "final_state": "tracking"}
        if self.state == MountState.TRACKING:
            self.state = MountState.UNPARKED
        return {"ok": True, "final_state": self.state.name.lower()}

    def stop(self) -> None:
        self.stop_calls += 1

    def _move(
        self,
        axis: str,
        direction: str,
        duration_ms: int,
        mode: str,
        rate_preset: int | None,
        cancel_check: Callable[[], bool] | None,
    ) -> AxisMotionResult:
        command = f"move_{axis}_{mode}"
        if not 20 <= duration_ms <= 120000:
            raise ValueError(f"{mode} duration must be between 20 and 120000 ms")
        if self.state == MountState.PARKED:
            raise _blocked("mount_parked", command)
        tracking = self.state == MountState.TRACKING
        if mode == "manual" and tracking:
            raise _blocked("manual_jog_requires_tracking_off", command)
        if mode == "center" and self.at_home:
            raise _blocked("axis_motion_refused_at_home", command)
        self.motion_calls.append(MotionCall(axis, direction, duration_ms, mode, rate_preset))
        cancelled = False
        deadline = time.monotonic() + self.simulate_motion_s
        while time.monotonic() < deadline:
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            time.sleep(0.005)
        return AxisMotionResult(
            ok=not cancelled,
            axis=axis,
            direction=direction[0],
            mode=mode,
            requested_arcsec=None,
            estimated_duration_ms=duration_ms,
            rate_preset=rate_preset,
            commands_sent=(),
            before_ra=0.0,
            before_dec=0.0,
            after_ra=0.0,
            after_dec=0.0,
            tracking_before=tracking,
            tracking_after=tracking,
            cancelled=cancelled,
        )

    def move_ra_timed(
        self,
        direction: str,
        duration_ms: int,
        *,
        mode: str = "center",
        rate_preset: int | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AxisMotionResult:
        return self._move("ra", direction, duration_ms, mode, rate_preset, cancel_check)

    def move_dec_timed(
        self,
        direction: str,
        duration_ms: int,
        *,
        mode: str = "center",
        rate_preset: int | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> AxisMotionResult:
        return self._move("dec", direction, duration_ms, mode, rate_preset, cancel_check)


class FakeOnStepFocuser:
    def __init__(self) -> None:
        self.position = 0
        self.max_position = 10000
        self.moving = False
        self.available = True
        self.reject_moves = False
        self.stop_calls = 0

    def connect(self) -> bool:
        return self.available

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return self.available

    def status(self) -> FocuserStatus:
        return FocuserStatus(self.available, self.position, self.max_position, self.moving)

    def get_position(self) -> int:
        return self.position

    def get_max_position(self) -> int:
        return self.max_position

    def is_moving(self) -> bool:
        return self.moving

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        start = self.position
        if self.reject_moves or not 0 <= steps <= self.max_position:
            return FocuserMoveResult(False, steps, start, "0")
        self.position = steps
        return FocuserMoveResult(True, steps, start, "1")

    def move(self, steps: int) -> None:
        self.position = max(0, min(self.max_position, self.position + steps))

    def stop(self) -> None:
        self.stop_calls += 1


@dataclass
class FakeOnStepClient:
    """Constructor-compatible with `OnStepClient(port, baud_rate=..., timeout=...)`."""

    port: str
    baud_rate: int = 9600
    timeout: float = 2.0
    mount: FakeOnStepMount = field(default_factory=FakeOnStepMount)
    focuser: FakeOnStepFocuser = field(default_factory=FakeOnStepFocuser)
    connect_ok: bool = True
    closed: bool = False
    connect_calls: int = 0

    def connect(self) -> object:
        self.connect_calls += 1
        self.closed = False
        ok = self.connect_ok and self.mount.connect()
        self.focuser.connect()

        class _Result:
            connected = ok
            mount_connected = ok
            focuser_available = self.focuser.available
            port = self.port

        return _Result()

    def close(self) -> None:
        self.closed = True
