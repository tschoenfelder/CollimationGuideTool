"""NoMountPark — MountParkPort stand-in for "no mount configured"."""

from __future__ import annotations

from astrotool_core.mount.park_port import MountParkPort, MountParkStatus


class NoMountPark(MountParkPort):
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return False

    def status(self) -> MountParkStatus:
        return MountParkStatus(available=False, parked=False, tracking=False)

    def park(self) -> None:
        pass

    def unpark(self) -> None:
        pass

    def stop_tracking(self) -> None:
        pass
