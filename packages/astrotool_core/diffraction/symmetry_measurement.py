"""Stage 6 focused-star symmetry / coma measurement — issue #20: "the
core maskless fine-collimation measurement". Determine whether the
focused diffraction pattern is rotationally symmetric and derive a
fine-collimation error direction. Domain-layer only, no UI language
anywhere in Stage 6's own requirements text.

#18's `RadialProfile` deliberately bins away angular information (a
pure function of radius) -- it cannot itself reveal asymmetry. This
module goes back to the stacked image + the already-measured center
(reused from `RadialProfileResult.center`, never re-detected) and
measures intensity around an annulus *by angle*.

#19's expected radius is a SOFT dependency, not a hard blocker: it only
helps place the analysis annulus when #18's own measured ring wasn't
resolvable. If the reference model is unavailable, this still proceeds
using #18's own `first_ring_radius_px` or a profile-derived fallback --
only #18's own invalidity (`sufficient_sampling=False`) makes the whole
measurement `INVALID`, since there's no center/profile to work from at
all in that case.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

import numpy as np

from astrotool_core.diffraction.optical_reference_model import DiffractionReferenceResult
from astrotool_core.diffraction.radial_profile import RadialProfileResult

#: Fallback analysis radius when neither #18's measured ring nor #19's
#: expected radius is available -- a fraction of the measured profile's
#: own outer extent, so the measurement degrades gracefully instead of
#: failing outright.
_FALLBACK_RADIUS_FRACTION = 0.6

#: Below this normalized magnitude, the pattern is classified as
#: fine-collimated ("asymmetry ~= zero") -- an implementation decision,
#: the requirements doc's own "exact algorithm is not prescribed"
#: allowance, same precedent #17-#19 already used.
_ASYMMETRY_TOLERANCE = 0.05

#: Confidence scale: mean per-sector signal above background, in
#: counts, at which confidence reaches 1.0. Tuned against this stage's
#: own synthetic fixtures (a solid, clearly-usable ring measures ~980
#: on this scale; a deliberately faint one measures ~4) -- also an
#: implementation decision.
_CONFIDENCE_SIGNAL_SCALE = 700.0

#: Below this confidence, the measurement is classified LOW_CONFIDENCE
#: (AC 6.4 -- "no screw recommendation issued").
_ACTIONABLE_CONFIDENCE_THRESHOLD = 0.3


class SymmetryStatus(Enum):
    FINE_COLLIMATED = "fine_collimated"
    ASYMMETRIC = "asymmetric"
    LOW_CONFIDENCE = "low_confidence"
    INVALID = "invalid"


@dataclass(frozen=True)
class SymmetryMeasurementResult:
    """`asymmetry_magnitude`/`asymmetry_direction_deg` are `None`
    exactly when `status` is `INVALID` -- populated (not null) for
    `LOW_CONFIDENCE` too, since the numbers were computable even if not
    actionable; only `status` (and any downstream logic) decides what
    to do with them."""

    status: SymmetryStatus
    asymmetry_magnitude: float | None
    asymmetry_direction_deg: float | None
    confidence: float
    reason: str | None


def _resolve_analysis_radius(
    profile_result: RadialProfileResult, reference_result: DiffractionReferenceResult
) -> float | None:
    if profile_result.first_ring_radius_px is not None:
        return profile_result.first_ring_radius_px
    if reference_result.available and reference_result.expected_first_null_radius_px is not None:
        return reference_result.expected_first_null_radius_px
    if profile_result.profile is not None and profile_result.profile.radii_px:
        return profile_result.profile.radii_px[-1] * _FALLBACK_RADIUS_FRACTION
    return None


def _sector_means(
    image: np.ndarray,
    center: tuple[float, float],
    radius: float,
    *,
    sector_count: int,
    band_width_px: float,
    background: float,
) -> tuple[list[float], list[float]]:
    """Background-subtracted mean intensity per angular sector, and the
    sector-center angles (radians, `atan2(dy, dx)` convention)."""
    height, width = image.shape
    yy, xx = np.indices((height, width), dtype=np.float64)
    r = np.hypot(xx - center[0], yy - center[1])
    theta = np.arctan2(yy - center[1], xx - center[0])
    band_mask = (r >= radius - band_width_px / 2.0) & (r < radius + band_width_px / 2.0)

    sector_width = 2.0 * math.pi / sector_count
    means: list[float] = []
    angles: list[float] = []
    for i in range(sector_count):
        angle_lo = -math.pi + i * sector_width
        angle_hi = angle_lo + sector_width
        sector_mask = band_mask & (theta >= angle_lo) & (theta < angle_hi)
        mean = float(np.mean(image[sector_mask])) - background if np.any(sector_mask) else 0.0
        means.append(mean)
        angles.append(angle_lo + sector_width / 2.0)
    return means, angles


def compute_symmetry_measurement(
    image: np.ndarray,
    profile_result: RadialProfileResult,
    reference_result: DiffractionReferenceResult,
    *,
    sector_count: int = 8,
    band_width_px: float = 3.0,
) -> SymmetryMeasurementResult:
    """`image` is the same stacked frame `profile_result` was computed
    from (Stage 3's own `StackResult.stacked`)."""
    if not profile_result.sufficient_sampling or profile_result.center is None:
        return SymmetryMeasurementResult(
            status=SymmetryStatus.INVALID, asymmetry_magnitude=None,
            asymmetry_direction_deg=None, confidence=0.0, reason=profile_result.reason,
        )

    radius = _resolve_analysis_radius(profile_result, reference_result)
    if radius is None:
        return SymmetryMeasurementResult(
            status=SymmetryStatus.INVALID, asymmetry_magnitude=None,
            asymmetry_direction_deg=None, confidence=0.0, reason="no_analysis_radius",
        )

    background = (
        profile_result.background_level if profile_result.background_level is not None else 0.0
    )
    means, angles = _sector_means(
        image, profile_result.center, radius,
        sector_count=sector_count, band_width_px=band_width_px, background=background,
    )

    total_flux = sum(means)
    vector_x = sum(mean * math.cos(angle) for mean, angle in zip(means, angles, strict=True))
    vector_y = sum(mean * math.sin(angle) for mean, angle in zip(means, angles, strict=True))

    magnitude = 0.0 if total_flux <= 0.0 else math.hypot(vector_x, vector_y) / total_flux
    direction_deg = math.degrees(math.atan2(vector_y, vector_x)) % 360.0

    mean_signal_above_background = total_flux / sector_count
    confidence = max(0.0, min(1.0, mean_signal_above_background / _CONFIDENCE_SIGNAL_SCALE))

    if confidence < _ACTIONABLE_CONFIDENCE_THRESHOLD:
        status = SymmetryStatus.LOW_CONFIDENCE
    elif magnitude <= _ASYMMETRY_TOLERANCE:
        status = SymmetryStatus.FINE_COLLIMATED
    else:
        status = SymmetryStatus.ASYMMETRIC

    return SymmetryMeasurementResult(
        status=status, asymmetry_magnitude=magnitude, asymmetry_direction_deg=direction_deg,
        confidence=confidence, reason=None,
    )
