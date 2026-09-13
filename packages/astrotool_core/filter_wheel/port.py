"""FilterWheelPort — hardware-independent electronic filter wheel (EFW)
status surface. Issue #34: read-only display of device-reported state,
deliberately no commanding method (no move/set-slot) -- the issue's own
"Do not couple this directly to capture logic" instruction and its
"redesigning filter-selection workflow" non-goal both scope commanding
a filter change as a separate, not-yet-decided concern.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class FilterWheelState:
    """`current_slot`/`filter_name` are `None` exactly when `available`
    is `False` or the position couldn't be read reliably -- `reason`
    explains why in that case, `None` when the state is nominal."""

    available: bool
    current_slot: int | None
    filter_name: str | None
    moving: bool
    reason: str | None


class FilterWheelPort(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def status(self) -> FilterWheelState: ...

    @property
    @abstractmethod
    def is_available(self) -> bool: ...
