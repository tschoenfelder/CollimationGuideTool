"""The one ms<->arcsec conversion for mounts that only move by angle (S6.0 / S6.0b).

Mount Align's sizing machinery and every `AxisResponse.duration_ms` / `px_per_ms` of a
calibration matrix speak durations. On a mount without a timed pulse primitive
(OnStepAdapter >= 0.4 over INDI: `capabilities().supports_pulse_guiding` is False) the moves
are angular, and S6.0 (787ceed) stores the matrix in **equivalent ms**: a duration that maps to
an angle at exactly `center_rate_x` x sidereal -- the same centering-rate seed `plan_first_move`
sizes the first move with. This is a unit conversion, not a mount-rate model: the measured
pixels stay authoritative.

Everything that converts such a duration to an angle or back -- Mount Align's panel and the
guide-assisted recenter policy -- goes through these functions, so the factor has exactly one
owner. The configured `center_rate_x` itself is owned by
`MountAlignmentSettings.calibration_center_rate_x`.
"""

from __future__ import annotations

import math

from astrotool_core.mount.movement_sizing import SizingPolicy, seed_rate_arcsec_per_s


def angular_unit_rate_arcsec_per_s(center_rate_x: float) -> float:
    """Arcsec per second of equivalent duration: `center_rate_x` x sidereal (identical, by
    construction, to the seed rate `plan_first_move` uses for that center rate)."""
    if not (math.isfinite(center_rate_x) and center_rate_x > 0.0):
        raise ValueError(f"invalid calibration center rate {center_rate_x!r}")
    return seed_rate_arcsec_per_s(SizingPolicy(center_rate_x=center_rate_x))


def equivalent_ms_to_arcsec(duration_ms: float, *, center_rate_x: float) -> float:
    """The angle an equivalent duration stands for."""
    return duration_ms * angular_unit_rate_arcsec_per_s(center_rate_x) / 1000.0


def arcsec_to_equivalent_ms(arcsec: float, *, center_rate_x: float) -> float:
    """The equivalent duration of an angle (unrounded; callers round in their own direction)."""
    return arcsec / angular_unit_rate_arcsec_per_s(center_rate_x) * 1000.0
