"""OnStepFocuserAdapter — `FocuserPort` over OnStepAdapter's `OnStepFocuser`."""

from __future__ import annotations

from onstep_adapter import OnStepFocuser

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

    def _focuser(self) -> OnStepFocuser | None:
        """The live focuser, or None when not connected or the controller has none."""
        client = self._connection.client
        if not self._held or client is None or not client.focuser.is_available:
            return None
        return client.focuser

    @property
    def is_available(self) -> bool:
        return self._focuser() is not None

    def status(self) -> FocuserStatus:
        focuser = self._focuser()
        if focuser is None:
            return _UNAVAILABLE
        s = focuser.status()
        return FocuserStatus(s.available, s.position, s.max_position, s.moving)

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        focuser = self._focuser()
        if focuser is None:
            return FocuserMoveResult(accepted=False, target_position=steps, start_position=0)
        r = focuser.move_absolute(steps)
        return FocuserMoveResult(r.accepted, r.target_position, r.start_position)

    def move(self, steps: int) -> None:
        focuser = self._focuser()
        if focuser is not None:
            focuser.move(steps)

    def get_position(self) -> int:
        focuser = self._focuser()
        return 0 if focuser is None else int(focuser.get_position())

    def get_max_position(self) -> int:
        focuser = self._focuser()
        return 0 if focuser is None else int(focuser.get_max_position())

    def is_moving(self) -> bool:
        focuser = self._focuser()
        return False if focuser is None else bool(focuser.is_moving())

    def stop(self) -> None:
        focuser = self._focuser()
        if focuser is not None:
            focuser.stop()
