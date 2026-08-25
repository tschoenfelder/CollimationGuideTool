"""MountPort — the shared, minimal INDI-style mount access surface.

This is the literal Protocol from collimation-guidetool-architektur.md
("Gemeinsamer INDI-Zugriff, getrennte Steuerungslogik"), deliberately far
smaller than smart_telescope's ``ports.mount.MountPort`` (no goto/park/
align/sync — collimation and guiding only ever need a bounded axis pulse).
The adapter knows how to move an axis; it never decides whether or how
much to move it — that is app-specific policy (CollimationRecenterPolicy /
GuideCorrectionPolicy), not this port.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol


class MountAxis(Enum):
    """AXIS1 = RA/azimuth, AXIS2 = Dec/altitude — matches the
    datasets/guiding/axis1_response, axis2_response naming (Stage 4)."""

    AXIS1 = auto()
    AXIS2 = auto()


class AxisDirection(Enum):
    POSITIVE = auto()
    NEGATIVE = auto()


@dataclass(frozen=True)
class MountCapabilities:
    supports_pulse_guiding: bool
    min_pulse_ms: int
    max_pulse_ms: int


@dataclass(frozen=True)
class MountStatus:
    connected: bool
    tracking: bool
    slewing: bool


@dataclass(frozen=True)
class CommandResult:
    accepted: bool
    message: str = ""


class MountPort(Protocol):
    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def capabilities(self) -> MountCapabilities: ...

    def status(self) -> MountStatus: ...

    def pulse_axis(
        self,
        axis: MountAxis,
        direction: AxisDirection,
        duration_ms: int,
    ) -> CommandResult: ...
