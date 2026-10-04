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
    """`supports_pulse_guiding`: `pulse_axis` performs timed moves at an installed rate (Mount
    Align chooses timed vs angular moves by it).

    `supports_guide_pulses_while_tracking` (S6.0d, #39): a SEPARATE capability -- the mount
    implements `GuidePulsePort`: bounded astronomical guide pulses that leave tracking ON
    (OnStepAdapter >= 0.5.0 `mount.guide_pulse`). It says nothing about `pulse_axis`, and
    Mount Align never reads it."""

    supports_pulse_guiding: bool
    min_pulse_ms: int
    max_pulse_ms: int
    supports_guide_pulses_while_tracking: bool = False


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


@dataclass(frozen=True)
class GuidePulseResult:
    """One guide pulse's outcome (S6.0d), normalized from OnStepAdapter 0.5.0's
    `IndiGuidePulseResult`.

    `accepted`: the whole pulse completed (and the mount was still tracking afterwards).
    `message`: why not -- the adapter's own refusal/failure text, never dropped.
    `sent`: at least one chunk reached the mount (0.5.0 `command_accepted`).
    `tracking_preserved`: 0.5.0's own field (its last snapshot's tracking flag; on a failure
    after an issued chunk that snapshot predates the emergency stop -- see `tracking_off`).
    `tracking_off`: tracking was observed OFF right after a pulse that had been sent and then
    failed -- e.g. 0.5.0's emergency stop (ABORT + TRACK_OFF) after a failed chunk, or a Stop.
    The caller must report this prominently: the mount no longer tracks.
    `warnings`: 0.5.0's warnings (e.g. "meridian_flip_required")."""

    accepted: bool
    message: str = ""
    sent: bool = False
    tracking_preserved: bool = False
    tracking_off: bool = False
    warnings: tuple[str, ...] = ()
    chunks_requested: int = 0
    chunks_completed: int = 0


class GuidePulsePort(Protocol):
    """Optional capability of a `MountPort` (`capabilities().supports_guide_pulses_while_tracking`):
    a bounded guide pulse on one axis/direction while the mount keeps tracking. `direction` is
    the axis direction in the same convention as `AngularMotionPort.move_angular` (AXIS1
    POSITIVE = west / increasing hour angle, AXIS2 POSITIVE = north); which way that moves the
    image is measured, never assumed."""

    @property
    def guide_pulse_range_ms(self) -> tuple[int, int] | None:
        """(min, max) duration of one `guide_pulse`, as the installed OnStepAdapter defines
        it; None without the capability."""
        ...

    def guide_pulse(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int
    ) -> GuidePulseResult: ...


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
