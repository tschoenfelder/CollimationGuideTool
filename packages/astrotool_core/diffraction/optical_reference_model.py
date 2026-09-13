"""Stage 5 optical diffraction reference model — issue #19: generate
the expected diffraction scale for the configured optical system,
calculated independently from any measured profile (Stage 4, #18).
Domain-layer only, no UI language anywhere in Stage 5's own
requirements text.

Physics: the classic Airy-disk first-null angular radius is
`theta = 1.22 * wavelength / aperture` (radians). On the sensor, linear
radius `r = theta * focal_length = 1.22 * wavelength * (focal_length /
aperture) = 1.22 * wavelength * focal_ratio` -- independent of aperture
and focal length individually, only their ratio (the focal ratio)
matters. With wavelength and pixel size both expressed in the same
unit (microns), this collapses to one clean formula with no unit-
conversion factor needed:

    expected_first_null_radius_px = 1.22 * wavelength_um * focal_ratio / pixel_size_um

Central obstruction is stored on `OpticalConfig` (the requirements
doc's own parameter list includes it, "where applicable") but
deliberately NOT used in this formula -- for typical amateur-telescope
obstruction ratios the first-null position itself barely shifts
(obstruction mainly redistributes energy into the surrounding rings,
not the null position); a materially more complex annular-aperture
model isn't justified by anything in the requirements doc yet. A scope
decision, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Airy-disk first-null angular-radius constant (radians * aperture / wavelength).
_AIRY_FIRST_NULL_COEFFICIENT = 1.22


@dataclass(frozen=True)
class OpticalConfig:
    """No field carries a silently-assumed default (issue's own "no
    fabricated default is silently substituted") -- every field is
    `None` unless explicitly supplied by the caller."""

    aperture_mm: float | None = None
    focal_length_mm: float | None = None
    #: Used directly if given; else derived from aperture_mm/focal_length_mm.
    focal_ratio: float | None = None
    pixel_size_um: float | None = None
    wavelength_nm: float | None = None
    #: Stored for future use/display -- not used in this stage's formula (see module docstring).
    central_obstruction_ratio: float | None = None


@dataclass(frozen=True)
class DiffractionReferenceResult:
    """`expected_first_null_radius_px`/`focal_ratio_used` are `None`
    exactly when `available` is `False` -- `reason` names the specific
    missing input that blocked the calculation."""

    expected_first_null_radius_px: float | None
    focal_ratio_used: float | None
    available: bool
    reason: str | None


def _resolve_focal_ratio(config: OpticalConfig) -> float | None:
    if config.focal_ratio is not None and config.focal_ratio > 0:
        return config.focal_ratio
    if (
        config.aperture_mm is not None
        and config.aperture_mm > 0
        and config.focal_length_mm is not None
        and config.focal_length_mm > 0
    ):
        return config.focal_length_mm / config.aperture_mm
    return None


def compute_diffraction_reference(config: OpticalConfig) -> DiffractionReferenceResult:
    """Checks pixel size, then focal ratio (direct, or derived from
    `aperture_mm`/`focal_length_mm` when both present), then
    wavelength -- the first missing input wins (AC 5.2's own single
    degraded-state shape; no need to enumerate every simultaneously-
    missing field)."""
    if config.pixel_size_um is None or config.pixel_size_um <= 0:
        return DiffractionReferenceResult(
            expected_first_null_radius_px=None, focal_ratio_used=None,
            available=False, reason="pixel_size_unavailable",
        )

    focal_ratio = _resolve_focal_ratio(config)
    if focal_ratio is None:
        return DiffractionReferenceResult(
            expected_first_null_radius_px=None, focal_ratio_used=None,
            available=False, reason="focal_ratio_unavailable",
        )

    if config.wavelength_nm is None or config.wavelength_nm <= 0:
        return DiffractionReferenceResult(
            expected_first_null_radius_px=None, focal_ratio_used=focal_ratio,
            available=False, reason="wavelength_unavailable",
        )

    wavelength_um = config.wavelength_nm / 1000.0
    radius_px = _AIRY_FIRST_NULL_COEFFICIENT * wavelength_um * focal_ratio / config.pixel_size_um
    return DiffractionReferenceResult(
        expected_first_null_radius_px=radius_px, focal_ratio_used=focal_ratio,
        available=True, reason=None,
    )
