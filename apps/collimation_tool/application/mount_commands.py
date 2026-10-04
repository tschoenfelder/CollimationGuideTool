"""Mount-command conventions shared by every operator command that moves the mount."""

from __future__ import annotations


def rearm_after_stop(mount: object) -> None:
    """S6.0c: a Stop latches in the mount adapter (`OnStepMountPulseAdapter.abort()`) and refuses
    every further move until the operator's NEXT command re-arms it with `clear_abort()`. Call
    this once a new command has been accepted -- Mount Align's runner on an accepted submit,
    guide-assisted reacquisition (S6.0b) when it is about to move -- never for a step of the
    sequence that Stop interrupted. Duck-typed: a mount without a stop latch needs nothing."""
    clear_abort = getattr(mount, "clear_abort", None)
    if callable(clear_abort):
        clear_abort()
