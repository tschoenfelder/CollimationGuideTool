"""FocuserPort — hardware-independent focuser control surface.

Ported from smart_telescope's ``ports.focuser.FocuserPort`` (kept as an
ABC, like CameraPort — both are direct ports of an existing smart_telescope
port; MountPort alone is a ``typing.Protocol`` because the architecture doc
specifies it verbatim that way). ``connect()`` changed to ``-> None``
(raise on failure) for consistency with MountPort/CameraPort.
``FocuserMoveResult.onstep_reply`` is dropped — it leaked an OnStep-specific
implementation detail into a hardware-neutral port; the OnStep adapter
(Stage 3) can still log it internally.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class FocuserStatus:
    available: bool
    position: int
    max_position: int
    moving: bool


@dataclass(frozen=True)
class FocuserMoveResult:
    accepted: bool
    target_position: int
    start_position: int


class FocuserPort(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def status(self) -> FocuserStatus: ...

    @abstractmethod
    def move_absolute(self, steps: int) -> FocuserMoveResult: ...

    @abstractmethod
    def move(self, steps: int) -> None: ...

    @abstractmethod
    def get_position(self) -> int: ...

    @abstractmethod
    def get_max_position(self) -> int: ...

    @abstractmethod
    def is_moving(self) -> bool: ...

    @abstractmethod
    def stop(self) -> None: ...

    @property
    @abstractmethod
    def is_available(self) -> bool: ...
