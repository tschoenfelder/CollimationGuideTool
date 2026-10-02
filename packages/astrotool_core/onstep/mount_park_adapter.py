"""OnStepMountParkAdapter — `MountParkPort` over OnStepAdapter's `IndiMount`
(>= 0.4.0, INDI-backed).

S6.0c: the two READS polled on the GUI thread (`status()`, `home_confirmed`)
never queue behind another thread's operation on the shared connection -- an
OnStepAdapter axis GOTO holds `operation_lock` for its whole blocking duration
(up to its 30 s timeout), which froze the Qt event loop on every Mount Align
move. When the lock is busy they serve their last reading instead of reading
the controller mid-operation (which 9cea2e9 forbids). Before any reading
exists they report the conservative "not known yet": unavailable / not
confirmed. Both are marked `fresh=False` (`MountParkStatus.fresh`), so a
decision -- the #44 tracking gate -- never treats them as verified.
Compound operations (park/unpark/tracking) still block on the lock.
"""

from __future__ import annotations

from dataclasses import replace

from onstep_adapter import IndiMount

from astrotool_core.mount.park_port import MountParkPort, MountParkStatus
from astrotool_core.onstep.connection import OnStepConnection

#: S6.0c: busy connection and nothing read yet -- "not known", never a guessed state.
_NOT_READ_YET = MountParkStatus(available=False, parked=False, tracking=False, fresh=False)


class OnStepMountParkAdapter(MountParkPort):
    def __init__(self, connection: OnStepConnection) -> None:
        self._connection = connection
        self._held = False
        #: S6.0c: the last readings, served while another operation holds the connection.
        self._last_status: MountParkStatus | None = None
        self._last_home_confirmed = False

    def connect(self) -> None:
        if not self._held:
            self._connection.acquire()
            self._held = True

    def disconnect(self) -> None:
        if self._held:
            self._held = False
            self._last_status, self._last_home_confirmed = None, False  # S6.0c: no stale carry-over
            self._connection.release()

    @property
    def is_available(self) -> bool:
        return self._mount() is not None

    def _mount(self) -> IndiMount | None:
        client = self._connection.client
        return client.mount if self._held and client is not None else None

    def status(self) -> MountParkStatus:
        # Real field report (diagnostic 7b21bdf1): see OnStepConnection's
        # own module docstring -- park()/unpark()/etc. below already run
        # on a worker thread (`long_running_actions`) while this status()
        # is polled from the GUI thread; serialize both against every
        # other adapter sharing this connection so they can never
        # interleave mid-sequence. S6.0c: ...without ever queueing behind one (module docstring).
        return self._read(wait_s=0.0)

    def decision_status(self, *, wait_fresh_s: float) -> MountParkStatus:
        """See `MountParkPort.decision_status` (S6.0c re-review R1)."""
        return self._read(wait_s=wait_fresh_s)

    def _read(self, *, wait_s: float) -> MountParkStatus:
        with self._connection.try_operation(wait_s) as entered:
            mount = self._mount()
            if mount is None:
                return MountParkStatus(available=False, parked=False, tracking=False)
            if not entered:
                last = self._last_status
                return _NOT_READ_YET if last is None else replace(last, fresh=False)
            snapshot = mount.get_status()
            self._last_status = MountParkStatus(
                available=True, parked=snapshot.parked, tracking=snapshot.tracking
            )
            return self._last_status

    def park(self) -> None:
        """Park via OnStepAdapter's own status-confirmed mechanical route --
        a REAL slew (verified via live status). As of OnStepAdapter 0.4.1
        it parks directly from any stationary, non-tracking state (no
        at-HOME precondition any more -- OnStepAdapter#16); already parked
        is an immediate success.

        Gated by `[indi].home_motion_enabled` in OnStepAdapter itself (off
        by default, pending its own supervised HOME test) -- that refusal
        surfaces here as the same `RuntimeError` a genuine park failure
        would, since `park()` never distinguished "refused" from "failed"
        for the caller even under 0.3.5."""
        with self._connection.operation_lock:
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
        with self._connection.operation_lock:
            mount = self._mount()
            if mount is None:
                return
            result = mount.unpark()
            if not result.unparked_confirmed:
                raise RuntimeError(
                    f"OnStep did not reach unparked with tracking off: {result.error}"
                )

    def stop_tracking(self) -> None:
        """No bare "tracking off" exists over INDI yet -- `emergency_stop`
        (abort + tracking off, confirmed) is this port's only available
        primitive and is at least as safe for this method's real caller
        (`MainWindow.closeEvent`'s "don't leave the mount moving after the
        app quits" safety net)."""
        with self._connection.operation_lock:
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
        calibration that this now resolves). With the default
        `[indi] tracking_authority_policy = "strict"` OnStepAdapter also
        demands a completed HOME slew and a time/site sync this app never
        performs, so a rig must set `"controller_managed"` (0.4.1) for this
        to ever succeed; either way it refuses parked/slewing/unsafe
        meridian states with its own reason; that refusal surfaces
        here as a `RuntimeError`, matching every other verified action on
        this port."""
        with self._connection.operation_lock:
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
        with self._connection.try_operation() as entered:  # S6.0c, as status()
            client = self._connection.client
            if not self._held or client is None:
                return False
            if not entered:
                return self._last_home_confirmed
            self._last_home_confirmed = bool(client.home_authority_established)
            return self._last_home_confirmed
