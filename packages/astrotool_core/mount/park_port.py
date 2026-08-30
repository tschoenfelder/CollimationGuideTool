"""MountParkPort — park/unpark-only mount control surface.

Deliberately separate from `MountPort` (`port.py`), not an extension of
it: that port is explicitly scoped to bounded axis pulses only ("no
goto/park/align/sync — collimation and guiding only ever need a bounded
axis pulse"), per the architecture doc's own stated design. Park/unpark
is a different, app-level concern (an operator explicitly parking or
unparking the mount around a session) with nothing to do with guiding or
collimation pulses, so it gets its own small port rather than growing
that one past its documented scope — matching the architecture doc's own
"shared INDI access, separate control logic" principle.

An ABC (not a `Protocol`, unlike `MountPort`): that one is a `Protocol`
only because the architecture doc specifies it verbatim that way; this
port has no such mandate, so it follows the more common pattern here
(`CameraPort`, `FocuserPort`).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class MountParkStatus:
    available: bool
    parked: bool
    tracking: bool


class MountParkPort(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def status(self) -> MountParkStatus: ...

    @abstractmethod
    def park(self) -> None: ...

    @abstractmethod
    def unpark(self) -> None:
        """Unpark, and — see the module this is implemented against for
        the real adapter's exact ordering — directly deactivate tracking
        too, rather than trusting the mount's own post-unpark default."""
        ...
