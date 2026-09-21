"""OnStepMountParkAdapter — `MountParkPort` over OnStepAdapter's `OnStepMount`."""

from __future__ import annotations

from onstep_adapter import OnStepMount
from onstep_adapter.ports.mount import MountState

from astrotool_core.mount.park_port import MountParkPort, MountParkStatus
from astrotool_core.onstep.connection import OnStepConnection


class OnStepMountParkAdapter(MountParkPort):
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

    @property
    def is_available(self) -> bool:
        return self._mount() is not None

    def _mount(self) -> OnStepMount | None:
        client = self._connection.client
        return client.mount if self._held and client is not None else None

    def status(self) -> MountParkStatus:
        mount = self._mount()
        if mount is None:
            return MountParkStatus(available=False, parked=False, tracking=False)
        state = mount.get_state()
        return MountParkStatus(
            available=True,
            parked=state == MountState.PARKED,
            tracking=state == MountState.TRACKING,
        )

    def park(self) -> None:
        mount = self._mount()
        if mount is not None and not mount.park():
            raise RuntimeError("OnStep did not accept the park command")

    def unpark(self) -> None:
        """Unpark and leave tracking OFF. OnStepAdapter verifies the final
        state itself instead of trusting the controller's post-unpark default."""
        mount = self._mount()
        if mount is None:
            return
        result = mount.recovery_unpark_stop_tracking()
        if not result.get("ok"):
            raise RuntimeError(f"OnStep unpark did not reach unparked, tracking-off: {result}")

    def stop_tracking(self) -> None:
        mount = self._mount()
        if mount is None:
            return
        result = mount.disable_tracking_verified()
        if not result.get("ok"):
            raise RuntimeError(f"OnStep still reports tracking after disable: {result}")

    def start_tracking(self) -> None:
        mount = self._mount()
        if mount is not None and not mount.enable_tracking():
            raise RuntimeError("OnStep did not accept the tracking-on command")
