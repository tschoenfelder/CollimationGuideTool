"""Autofocus hard safety envelope — issue #33's own most emphasized
requirement: every commanded position during an autofocus run must satisfy
BOTH the focuser/device's own configured limits AND a hard search envelope
of at most ±1000 steps from the run's own starting position, whichever is
tighter.

``FocuserPort`` (see ``astrotool_core.focus.port``) exposes only a device
maximum (``max_position`` / ``get_max_position()``) — no device minimum
exists anywhere in this codebase, real or fake. ``device_min_position``
therefore defaults to ``0`` here, a documented, conservative assumption
(matching how virtually every real absolute-position focuser behaves), not
a value read from any real hardware limit.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Issue #33's own hard search envelope: no autofocus algorithm may command
#: a position more than this many steps away from where the run started,
#: regardless of what the device's own limits would otherwise allow.
DEFAULT_ENVELOPE_STEPS = 1000


@dataclass(frozen=True)
class FocuserSearchBounds:
    """The intersection of a device's own limits and the autofocus-local
    ±envelope for one run -- the only positions any search step may ever
    command."""

    allowed_min: int
    allowed_max: int

    def contains(self, position: int) -> bool:
        return self.allowed_min <= position <= self.allowed_max

    def clamp(self, position: int) -> int:
        return max(self.allowed_min, min(self.allowed_max, position))


def compute_search_bounds(
    start_position: int,
    device_max_position: int,
    *,
    device_min_position: int = 0,
    envelope_steps: int = DEFAULT_ENVELOPE_STEPS,
) -> FocuserSearchBounds:
    """``allowed_min = max(device_min_position, start_position -
    envelope_steps)``; ``allowed_max = min(device_max_position,
    start_position + envelope_steps)`` -- issue #33's own pseudocode,
    verbatim."""
    allowed_min = max(device_min_position, start_position - envelope_steps)
    allowed_max = min(device_max_position, start_position + envelope_steps)
    return FocuserSearchBounds(allowed_min=allowed_min, allowed_max=allowed_max)
