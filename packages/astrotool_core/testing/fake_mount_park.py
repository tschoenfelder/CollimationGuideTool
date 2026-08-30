"""FakeMountPark — MountParkPort test/dev double with simulated park state."""

from __future__ import annotations

from astrotool_core.mount.park_port import MountParkPort, MountParkStatus


class FakeMountPark(MountParkPort):
    def __init__(
        self, *, fail_connect: bool = False, available: bool = True, start_parked: bool = True
    ) -> None:
        self._fail_connect = fail_connect
        self._available = available
        self._parked = start_parked
        self._tracking = False

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("FakeMountPark: connect failed (simulated)")

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return self._available

    def status(self) -> MountParkStatus:
        return MountParkStatus(
            available=self._available, parked=self._parked, tracking=self._tracking
        )

    def park(self) -> None:
        self._parked = True

    def unpark(self) -> None:
        self._parked = False
        self._tracking = False  # see MountParkPort.unpark's docstring
