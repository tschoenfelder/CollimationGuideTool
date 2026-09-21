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
        *,
        rate_preset: str | None = None,
    ) -> CommandResult:
        """`rate_preset` is adapter-specific (an OnStep rate-preset digit "0"-"9"
        on `OnStepMountPulseAdapter`) and optional -- `None`
        means "whatever rate the adapter would otherwise use," so existing
        callers that never pass it keep their current behavior unchanged.
        Added for the mount-alignment feature, which needs every calibration
        and nudge pulse to run at one deliberately-chosen rate regardless of
        an adapter's own default."""
        ...


class AngularMotionPort(Protocol):
    """Optional capability of a `MountPort`: angular moves with runtime-installed rates.

    `OnStepMountPulseAdapter` provides it over OnStepAdapter's `move_ra`/`move_dec` +
    `set_motion_calibration` (>= 0.3.5). The application decides the angular size (image
    space -> optics -> arcsec) and measures the achieved displacement; rates are
    established by a timed bootstrap move (`MountPort.pulse_axis` with no rate preset,
    i.e. OnStep's center rate), measured from camera displacement, then installed here.
    `direction` is the axis direction, `arcsec` a positive magnitude."""

    def installed_rate(self, axis: MountAxis, direction: AxisDirection) -> float | None:
        """Installed centering rate (arcsec/s) for this axis direction, or None."""
        ...

    def install_rate(self, axis: MountAxis, direction: AxisDirection, arcsec_per_s: float) -> None:
        """Install one MEASURED centering rate; other directions' rates are kept."""
        ...

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        """Move by `arcsec` through OnStepAdapter's angular API; `accepted=False` (with
        the adapter's reason) if refused or no rate is installed for the direction."""
        ...
