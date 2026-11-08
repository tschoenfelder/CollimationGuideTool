"""Wait for an OnStepAdapter mount move to end.

OnStepAdapter's `park()`, `go_home()` and finite axis moves return a `MotionHandle` at once
(OnStepAdapter after 0.6.2, `indi_motion.py`); `wait()` blocks until the move ends and returns
the result the call returned before. The shims here keep their blocking contract -- each move
still runs under `OnStepConnection.operation_lock` until the mount is done -- so they wait on
the handle. A result that is not a handle (the test doubles' direct results) is returned as is.

A move that is stopped early (`MotionHandle.stop()`, the mount's `stop()`, the meridian
supervisor) makes `wait()` raise `MotionStopped`, which is not one of
`ONSTEP_OPERATION_ERRORS`; it is re-raised as `MoveStopped`, a `RuntimeError`, so every shim
reports it like any other device-side refusal.
"""

from __future__ import annotations

from typing import Any

from onstep_adapter.indi_motion import MotionStopped


class MoveStopped(RuntimeError):
    """The mount move was stopped before it finished."""


def finish_motion(started: object) -> Any:  # noqa: ANN401 -- OnStepAdapter's results are untyped
    """`started.wait()` for a `MotionHandle`, `started` itself otherwise."""
    wait = getattr(started, "wait", None)
    if not callable(wait):
        return started
    try:
        return wait()
    except MotionStopped as exc:
        raise MoveStopped(str(exc)) from exc


__all__ = ["MoveStopped", "finish_motion"]
