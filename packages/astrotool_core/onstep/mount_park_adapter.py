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
        """Park the field-proven way: route to mechanical HOME, settle, then PARK -- each
        step proven by OnStep's own status flags (OnStepAdapter's `park_via_home`)."""
        mount = self._mount()
        if mount is None:
            return
        result = mount.park_via_home()
        if not result.get("ok"):
            raise RuntimeError(f"OnStep did not reach the parked state: {result.get('reason')}")

    #: `park()`/`unpark()` take seconds to minutes (the mount slews); a UI must not call
    #: them on its GUI thread (`MountParkPanel` runs them on a worker when this is set).
    long_running_actions = True

    def unpark(self) -> None:
        """Unpark, leave tracking OFF and drive the mount to its mechanical HOME.

        A plain unpark leaves the mount at its PARK position, which is not home; OnStepAdapter's
        `unpark_to_home_stop_tracking` does the whole sequence and is confirmed by OnStep's
        at-home flag (up to ~45 s), not by an acknowledgement. It needs no trusted clock or
        location, and no home confirmation (that comes AFTER, from the operator)."""
        mount = self._mount()
        if mount is None:
            return
        result = mount.unpark_to_home_stop_tracking()
        if not result.get("ok"):
            raise RuntimeError(
                "OnStep did not reach home unparked with tracking off "
                f"(at_home={result.get('at_home')}, final_state={result.get('final_state')})"
            )

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

    def confirm_home(self) -> None:
        """The OPERATOR confirms the mount is physically at its mechanical home.

        OnStepAdapter refuses every motion (`mechanical_position_authority_untrusted`) until
        this is done; it must be an explicit human action, never automatic. Not part of
        `MountParkPort` (like `abort()` it is an adapter capability the panel duck-types)."""
        mount = self._mount()
        if mount is None:
            raise ConnectionError("OnStep mount is not connected")
        mount.confirm_home_position()

    @property
    def home_confirmed(self) -> bool:
        mount = self._mount()
        return bool(mount is not None and mount.safety_snapshot().get("home_confirmed"))
