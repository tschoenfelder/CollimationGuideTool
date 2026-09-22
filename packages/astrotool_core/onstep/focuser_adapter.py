"""OnStepFocuserAdapter — `FocuserPort` over OnStepAdapter's `IndiFocuser`
(>= 0.4.0, INDI-backed)."""

from __future__ import annotations

import contextlib

from onstep_adapter import IndiFocuser

from astrotool_core.focus.port import FocuserMoveResult, FocuserPort, FocuserStatus
from astrotool_core.onstep.connection import OnStepConnection

_UNAVAILABLE = FocuserStatus(available=False, position=0, max_position=0, moving=False)


class OnStepFocuserAdapter(FocuserPort):
    def __init__(self, connection: OnStepConnection) -> None:
        self._connection = connection
        self._held = False

    def connect(self) -> None:
        if not self._held:
            self._connection.acquire()
            self._held = True

    def disconnect(self) -> None:
        if self._held:
            self._held = False
            self._connection.release()

    def _focuser(self) -> IndiFocuser | None:
        """The live focuser, or None when not connected."""
        client = self._connection.client
        if not self._held or client is None or client.focuser is None:
            return None
        return client.focuser

    @property
    def is_available(self) -> bool:
        return self.status().available

    def status(self) -> FocuserStatus:
        focuser = self._focuser()
        if focuser is None:
            return _UNAVAILABLE
        s = focuser.get_status()
        available = "indi_device_disconnected" not in s.blockers and s.position is not None
        maximum = s.driver_maximum or s.configured_maximum or 0
        return FocuserStatus(available, s.position or 0, maximum, bool(s.moving))

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        focuser = self._focuser()
        if focuser is None:
            return FocuserMoveResult(accepted=False, target_position=steps, start_position=0)
        start = focuser.get_status().position or 0
        try:
            r = focuser.move_absolute(steps)
        except ValueError:
            return FocuserMoveResult(accepted=False, target_position=steps, start_position=start)
        return FocuserMoveResult(r.reached, r.target, start)

    def move(self, steps: int) -> None:
        """Relative move — 0.4.0's `IndiFocuser` only exposes an absolute
        target, so this synthesizes one from the last known position."""
        focuser = self._focuser()
        if focuser is None:
            return
        current = focuser.get_status().position or 0
        maximum = focuser.get_status().driver_maximum or focuser.get_status().configured_maximum
        target = current + steps
        if maximum is not None:
            target = max(0, min(maximum, target))
        with contextlib.suppress(ValueError):
            focuser.move_absolute(target)

    def get_position(self) -> int:
        focuser = self._focuser()
        return 0 if focuser is None else int(focuser.get_status().position or 0)

    def get_max_position(self) -> int:
        focuser = self._focuser()
        if focuser is None:
            return 0
        s = focuser.get_status()
        return int(s.driver_maximum or s.configured_maximum or 0)

    def is_moving(self) -> bool:
        focuser = self._focuser()
        return False if focuser is None else bool(focuser.get_status().moving)

    def stop(self) -> None:
        focuser = self._focuser()
        if focuser is not None:
            focuser.stop()
