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

    @abstractmethod
    def stop_tracking(self) -> None:
        """Deactivate tracking *without* parking -- for a caller that
        wants the mount to stop moving right now (real use: MainWindow's
        closeEvent, so tracking doesn't keep running unattended after the
        app quits) but shouldn't also drive a park slew on its own
        initiative -- parking stays a deliberate, explicit user action via
        the Mount panel, same as it already is everywhere else in this
        app. Safe to call whether or not the mount is currently tracking,
        and a no-op if not available."""
        ...

    @abstractmethod
    def start_tracking(self) -> None:
        """Activate tracking -- issue #30: star-mode calibration requires
        tracking ON before a BEFORE frame is captured
        (`astrotool_core.mount.tracking_mode.ensure_tracking_mode`), which
        needs a way to *establish* that mode, not just observe/clear it
        (this port previously only ever had `stop_tracking`). Safe to
        call whether or not the mount is already tracking, and a no-op if
        not available -- same contract as `stop_tracking`."""
        ...
