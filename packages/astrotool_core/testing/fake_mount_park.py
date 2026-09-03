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
        #: How many times park()/unpark() were actually called -- e.g. lets
        #: MountTestMoveRunner.submit_sequence's tests confirm a multi-step
        #: sequence unparks exactly once, not once per step.
        self.park_count = 0
        self.unpark_count = 0
        self.stop_tracking_count = 0
        self.start_tracking_count = 0

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
        self.park_count += 1
        self._parked = True

    def unpark(self) -> None:
        self.unpark_count += 1
        self._parked = False
        self._tracking = False  # see MountParkPort.unpark's docstring

    def stop_tracking(self) -> None:
        self.stop_tracking_count += 1
        self._tracking = False

    def start_tracking(self) -> None:
        self.start_tracking_count += 1
        self._tracking = True
