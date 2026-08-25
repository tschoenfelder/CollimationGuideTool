"""FakeFocuser — FocuserPort test/dev double with simulated movement.

Ported from smart_telescope's ``adapters.mock.focuser.MockFocuser``.
"""

from __future__ import annotations

from astrotool_core.focus.port import FocuserMoveResult, FocuserPort, FocuserStatus

_MAX_POSITION = 5000


class FakeFocuser(FocuserPort):
    def __init__(self, *, fail_connect: bool = False, available: bool = True) -> None:
        self._fail_connect = fail_connect
        self._available = available
        self._position = 0

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("FakeFocuser: connect failed (simulated)")

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return self._available

    def status(self) -> FocuserStatus:
        return FocuserStatus(
            available=self._available,
            position=self._position if self._available else 0,
            max_position=self.get_max_position(),
            moving=False,
        )

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        start = self._position
        self._position = steps
        return FocuserMoveResult(accepted=True, target_position=steps, start_position=start)

    def move(self, steps: int) -> None:
        self._position += steps

    def get_position(self) -> int:
        return self._position

    def get_max_position(self) -> int:
        return _MAX_POSITION

    def is_moving(self) -> bool:
        return False

    def stop(self) -> None:
        pass
