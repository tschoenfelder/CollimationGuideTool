"""OnStepMountParkAdapter — `MountParkPort` over OnStepAdapter's `IndiMount`
(>= 0.4.0, INDI-backed)."""

from __future__ import annotations

from onstep_adapter import IndiMount

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

    def _mount(self) -> IndiMount | None:
        client = self._connection.client
        return client.mount if self._held and client is not None else None

    def status(self) -> MountParkStatus:
        mount = self._mount()
        if mount is None:
            return MountParkStatus(available=False, parked=False, tracking=False)
        snapshot = mount.get_status()
        return MountParkStatus(
            available=True, parked=snapshot.parked, tracking=snapshot.tracking
        )

    def park(self) -> None:
        """Park via OnStepAdapter's own status-confirmed mechanical route --
        a REAL slew (verified via live status), requiring the mount
        already be at a confirmed HOME first.

        Gated by `[indi].home_motion_enabled` in OnStepAdapter itself (off
        by default, pending its own supervised HOME test) -- that refusal
        surfaces here as the same `RuntimeError` a genuine park failure
        would, since `park()` never distinguished "refused" from "failed"
        for the caller even under 0.3.5."""
        mount = self._mount()
        if mount is None:
            return
        result = mount.park()
        if not result.confirmed:
            raise RuntimeError(f"OnStep did not reach the parked state: {result.error}")

    #: `park()` slews (seconds to minutes); `unpark()` is a quick switch
    #: flip + confirmation poll, not a slew, but both still make a
    #: blocking INDI round trip a UI must not run on its GUI thread
    #: (`MountParkPanel` runs them on a worker when this is set).
    long_running_actions = True

    def unpark(self) -> None:
        """Leave PARKED with tracking off -- a plain unpark-then-track-off
        switch sequence, confirmed via live status. **Not a slew** --
        despite living behind
        the same `home_motion_enabled` gate as `park()`/`go_home()` (which
        genuinely do slew), `unpark()` itself never requests HOME or moves
        the mount at all (`indi_home.py`'s `IndiHomeRouter.unpark`'s own
        docstring: "never request HOME"). An earlier version of this
        docstring wrongly claimed it drove to HOME -- corrected after a
        real-field report questioned why a harmless unpark needed gating
        at all; the answer is it's bundled with two operations that do
        need it, not that unpark itself is risky."""
        mount = self._mount()
        if mount is None:
            return
        result = mount.unpark()
        if not result.unparked_confirmed:
            raise RuntimeError(f"OnStep did not reach unparked with tracking off: {result.error}")

    def stop_tracking(self) -> None:
        """No bare "tracking off" exists over INDI yet -- `emergency_stop`
        (abort + tracking off, confirmed) is this port's only available
        primitive and is at least as safe for this method's real caller
        (`MainWindow.closeEvent`'s "don't leave the mount moving after the
        app quits" safety net)."""
        mount = self._mount()
        if mount is None:
            return
        result = mount.stop()
        if not result.stopped_confirmed:
            raise RuntimeError(f"OnStep still reports motion after stop: {result.errors}")

    def start_tracking(self) -> None:
        """Enable tracking through OnStepAdapter's verified INDI route
        (`IndiMount.enable_tracking`, added after OnStepAdapter#14 --
        0.4.0 originally shipped with this always raising
        `NotImplementedError`, a real regression for issue #30's star-mode
        calibration that this now resolves). OnStepAdapter refuses from an
        unsafe/untrusted state (parked, at home, slewing, an unsafe
        meridian phase, ...) with its own reason; that refusal surfaces
        here as a `RuntimeError`, matching every other verified action on
        this port."""
        mount = self._mount()
        if mount is None:
            return
        result = mount.enable_tracking()
        if not result.tracking_confirmed:
            raise RuntimeError(f"OnStep did not confirm tracking on: {result.error}")

    #: No `confirm_home()` method (unlike 0.3.5): >= 0.4.0 establishes home
    #: authority automatically from live status
    #: (`OnStepIndiClient.home_authority_established`), not from a manual
    #: operator action, so `MountParkPanel` correctly auto-hides its
    #: "Confirm at home" button here (`hasattr(mount, "confirm_home")` is
    #: False) rather than showing a control that would do nothing. The
    #: read-only status this enables is still useful, so it stays exposed.
    @property
    def home_confirmed(self) -> bool:
        client = self._connection.client
        return bool(self._held and client is not None and client.home_authority_established)
