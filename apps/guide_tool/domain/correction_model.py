"""GuideCorrectionPolicy's pure decision core: given a measured guide
error and calibrated axis responses, decide whether/how much to pulse
each axis.

Ported from smart_telescope's `services/guide_measurement.py::
MeasureOnlyGuideController`, redesigned to consume Stage 4's empirically
measured `CalibrationMatrix` (`duration_ms = |error_px| * aggressiveness
/ measured_px_per_ms`) instead of a fixed `ms_per_px` rate guess, and to
target `MountAxis`/`AxisDirection` instead of "ra"/"dec" + "n"/"s"/"e"/"w"
direction strings. Direction is picked by the calibration's *measured*
sign (whichever direction's calibrated response opposes the error) rather
than an assumed image-orientation convention — same reasoning as
`collimation_tool.application.recenter_policy`'s `_direction_opposing`,
independently re-derived here rather than imported: `guide_tool` must
never depend on `collimation_tool` (see CONTRIBUTING.md's dependency rule),
and keeping this tiny calculation independent per app is also exactly
what stops a change to one app's correction logic from ever leaking into
the other's.
"""

from __future__ import annotations

from dataclasses import dataclass

from astrotool_core.mount.axis_calibration import CalibrationMatrix
from astrotool_core.mount.port import AxisDirection, MountAxis


@dataclass(frozen=True)
class WouldGuidePulse:
    axis: MountAxis
    direction: AxisDirection
    duration_ms: int
    reason: str


@dataclass(frozen=True)
class GuideCorrectionConfig:
    deadband_px: float = 0.5
    max_pulse_ms: int = 2000
    min_pulse_ms: int = 50
    aggressiveness: float = 0.7
    axis2_enabled: bool = True  # equivalent of the source's `not ra_only`


def compute_would_pulses(
    error_x: float,
    error_y: float,
    calibration: CalibrationMatrix,
    config: GuideCorrectionConfig,
) -> list[WouldGuidePulse]:
    """Compute the guide pulses that would correct (error_x, error_y).

    Returns one entry per axis whose error exceeds the deadband and whose
    corrective pulse would be at least ``min_pulse_ms`` long. Pure — issues
    no mount calls; see `guide_tool.application.correction_policy` for that.
    """
    axes: list[tuple[MountAxis, float]] = [(MountAxis.AXIS1, error_x)]
    if config.axis2_enabled:
        axes.append((MountAxis.AXIS2, error_y))

    pulses: list[WouldGuidePulse] = []
    for axis, error in axes:
        if abs(error) <= config.deadband_px:
            continue
        direction = _direction_opposing(axis, error, calibration)
        axis_response = calibration.response_for(axis, direction)
        px_per_ms = max(axis_response.px_per_ms, 1e-9)
        raw_ms = abs(error) * config.aggressiveness / px_per_ms
        if raw_ms < config.min_pulse_ms:
            continue
        duration_ms = min(round(raw_ms), config.max_pulse_ms)
        pulses.append(
            WouldGuidePulse(
                axis=axis,
                direction=direction,
                duration_ms=duration_ms,
                reason=f"{axis.name.lower()}_error",
            )
        )
    return pulses


def _direction_opposing(
    axis: MountAxis, error: float, calibration: CalibrationMatrix
) -> AxisDirection:
    positive_response = calibration.response_for(axis, AxisDirection.POSITIVE)
    positive_component = (
        positive_response.dx_px if axis is MountAxis.AXIS1 else positive_response.dy_px
    )
    if (positive_component > 0) != (error > 0):
        return AxisDirection.POSITIVE
    return AxisDirection.NEGATIVE
