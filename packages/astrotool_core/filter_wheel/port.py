"""FilterWheelPort — hardware-independent electronic filter wheel (EFW)
control surface. Issue #34 shipped read-only display of device-reported
state; issue #47 adds commanding a slot change.
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

    @abstractmethod
    def set_slot(self, slot: int) -> None:
        """Command the wheel to `slot`. Returns immediately -- poll `status()`
        for `moving`/`current_slot` to observe progress and confirm arrival
        (issue #47: never treat the requested slot as current until the
        device actually reports it). Raises RuntimeError if a move is
        already in progress (never issue a second move while one is in
        flight, unless a future adapter can confirm its driver replaces the
        target safely -- this base contract does not assume that). A no-op
        when not connected/available, matching this port's existing
        tolerance elsewhere."""
        ...

    def slot_names(self) -> dict[int, str]:
        """Best-known name for every slot this wheel has, by precedence:
        device-reported name, then any config-supplied fallback, else absent
        (the caller shows a bare slot number). Default: empty (no fallback
        source, no way to enumerate slots) -- `IndiFilterWheelAdapter`
        overrides this with a real answer."""
        return {}
