"""Mount-command conventions shared by every operator command that moves the mount."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class StopLatch(Protocol):
    """S6.8d (F13): a mount adapter with a latched Stop -- `abort()` latches it (lock-free, any
    thread), `clear_abort()` re-arms it (`OnStepMountPulseAdapter`). A typed port instead of a
    `getattr` probe; `MountCapabilities` has no field for it (its field set is frozen)."""

    def abort(self) -> None: ...

    def clear_abort(self) -> None: ...


def rearm_after_stop(mount: object) -> None:
    """S6.0c: a Stop latches in the mount adapter (`OnStepMountPulseAdapter.abort()`) and refuses
    every further move until the operator's NEXT command re-arms it with `clear_abort()`. Call
    this once a new command has been accepted -- Mount Align's runner on an accepted submit,
    guide-assisted reacquisition (S6.0b) when it is about to move -- never for a step of the
    sequence that Stop interrupted. A mount that is no `StopLatch` needs nothing."""
    if isinstance(mount, StopLatch):
        mount.clear_abort()
