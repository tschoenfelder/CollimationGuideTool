"""NoFocuser — FocuserPort stand-in for "no focuser configured"."""

from __future__ import annotations

from astrotool_core.focus.port import FocuserMoveResult, FocuserPort, FocuserStatus


class NoFocuser(FocuserPort):
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return False

    def status(self) -> FocuserStatus:
        return FocuserStatus(available=False, position=0, max_position=0, moving=False)

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        return FocuserMoveResult(accepted=False, target_position=steps, start_position=0)

    def move(self, steps: int) -> None:
        pass

    def get_position(self) -> int:
        return 0

    def get_max_position(self) -> int:
        return 0

    def is_moving(self) -> bool:
        return False

    def stop(self) -> None:
        pass
