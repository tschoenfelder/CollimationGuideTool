"""StarFieldRegistrar — issue #29's astronomical cross-camera matcher:
solves each frame independently via ASTAP/WCS (`astap_adapter`) and
derives relative FOV geometry directly from the two sky solutions,
without ever needing the two frames to share any pixel-level content —
the key capability terrestrial matching structurally cannot offer (issue
#29 #4: "A successful plate solution can determine relative sky geometry
even when the two FOVs do not overlap").

Deliberately depends only on `AstapSolver`'s small Protocol, not
`AstapCliSolver` directly — every test here builds synthetic
`astropy.wcs.WCS` objects and a fake solver, never touching a real ASTAP
process (see `astap_adapter`'s own docstring for that boundary).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from astropy.coordinates import SkyCoord
from astropy.wcs import WCS

from astrotool_core.registration.astap_adapter import (
    AstapSolveHint,
    AstapSolver,
    AstapSolveStatus,
)
from astrotool_core.registration.geometry import Polygon, overlap_polygon, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)


@dataclass(frozen=True)
class StarFieldHints:
    """Optional per-frame ASTAP hints — see `AstapSolveHint`'s own
    docstring on why a previous registration is only ever a hint, never a
    substitute for solving (issue #29 #3)."""

    hint_a: AstapSolveHint | None = None
    hint_b: AstapSolveHint | None = None


class StarFieldRegistrar:
    """Cross-camera registration for real star fields — see this
    module's own docstring."""

    def __init__(self, solver: AstapSolver) -> None:
        self._solver = solver

    def register(
        self,
        frame_a_path: Path,
        frame_b_path: Path,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        *,
        hints: StarFieldHints | None = None,
    ) -> CrossCameraRegistrationResult:
        """`frame_a_path`/`frame_b_path` are FITS files (ASTAP solves
        files on disk, not in-memory arrays — see `astap_adapter`).
        Issue #29 #4's own outcome list: a solved, non-overlapping pair
        is `OK_NO_OVERLAP` (real, valid geometry), never a failure
        status; only an actual solver failure produces one of the
        `SOLVE_FAILED_*`/`ASTAP_UNAVAILABLE`/`INVALID_SOLUTION` statuses.
        """
        resolved_hints = hints or StarFieldHints()
        solve_a = self._solver.solve(frame_a_path, hint=resolved_hints.hint_a)
        if solve_a.status is AstapSolveStatus.ASTAP_UNAVAILABLE:
            return _failure(RegistrationStatus.ASTAP_UNAVAILABLE, message=solve_a.message)
        if not solve_a.ok:
            return _failure(RegistrationStatus.SOLVE_FAILED_A, message=solve_a.message)

        solve_b = self._solver.solve(frame_b_path, hint=resolved_hints.hint_b)
        if solve_b.status is AstapSolveStatus.ASTAP_UNAVAILABLE:
            return _failure(RegistrationStatus.ASTAP_UNAVAILABLE, message=solve_b.message)
        if not solve_b.ok:
            return _failure(RegistrationStatus.SOLVE_FAILED_B, message=solve_b.message)

        assert solve_a.wcs is not None and solve_b.wcs is not None  # guaranteed by .ok
        return geometry_from_wcs(solve_a.wcs, prior_a, solve_b.wcs, prior_b)


def _failure(status: RegistrationStatus, *, message: str) -> CrossCameraRegistrationResult:
    return CrossCameraRegistrationResult(
        method=RegistrationMethod.STAR_FIELD, status=status, diagnostics={"message": message},
    )


def geometry_from_wcs(
    wcs_a: WCS, prior_a: OpticalPrior, wcs_b: WCS, prior_b: OpticalPrior
) -> CrossCameraRegistrationResult:
    """The fully pure, ASTAP-free core: given two already-solved WCS
    solutions, project optical train A's own sensor rectangle (from
    `prior_a`) through the sky into B's pixel frame. This is the one
    piece of the module directly unit-testable with synthetic WCS
    objects (no ASTAP process anywhere) — see
    `tests/core/registration/test_star_field_registrar.py`.
    """
    corners_px_a = (
        (0.0, 0.0),
        (float(prior_a.sensor_width_px), 0.0),
        (float(prior_a.sensor_width_px), float(prior_a.sensor_height_px)),
        (0.0, float(prior_a.sensor_height_px)),
    )
    sky_corners: list[SkyCoord] = [wcs_a.pixel_to_world(x, y) for x, y in corners_px_a]
    try:
        polygon_a_in_b: Polygon = tuple(
            _pixel_point(wcs_b, sky) for sky in sky_corners
        )
    except (ValueError, TypeError) as exc:
        return _failure(RegistrationStatus.INVALID_SOLUTION, message=str(exc))

    if any(not math.isfinite(x) or not math.isfinite(y) for x, y in polygon_a_in_b):
        return _failure(
            RegistrationStatus.INVALID_SOLUTION,
            message="WCS projection produced a non-finite pixel coordinate",
        )

    rotation_deg, scale = _rotation_and_scale(polygon_a_in_b, prior_a.sensor_width_px)

    frame_b_rect = rect_polygon(
        prior_b.sensor_width_px, prior_b.sensor_height_px,
        center=(prior_b.sensor_width_px / 2.0, prior_b.sensor_height_px / 2.0),
    )
    overlap = overlap_polygon(polygon_a_in_b, frame_b_rect)
    status = RegistrationStatus.OK_OVERLAP if overlap else RegistrationStatus.OK_NO_OVERLAP
    return CrossCameraRegistrationResult(
        method=RegistrationMethod.STAR_FIELD,
        status=status,
        rotation_deg=rotation_deg,
        scale=scale,
        polygon_a_in_b=polygon_a_in_b,
        overlap_polygon=overlap,
        diagnostics={"prior_a": prior_a.name, "prior_b": prior_b.name},
    )


def _pixel_point(wcs: WCS, sky: SkyCoord) -> tuple[float, float]:
    x, y = wcs.world_to_pixel(sky)
    return float(x), float(y)


def _rotation_and_scale(polygon_a_in_b: Polygon, sensor_width_px: int) -> tuple[float, float]:
    """`rotation_deg`/`scale` from the projected top edge (corner 0 ->
    corner 1, A's own +x/"width" direction) — same convention as
    `terrestrial_registrar`'s own result fields (image-space forward
    rotation, B-pixels per one A-pixel), so a caller doesn't need to
    treat the two methods' results differently."""
    (x0, y0), (x1, y1) = polygon_a_in_b[0], polygon_a_in_b[1]
    dx, dy = x1 - x0, y1 - y0
    edge_len_b = math.hypot(dx, dy)
    rotation_deg = math.degrees(math.atan2(dy, dx))
    scale = edge_len_b / sensor_width_px if sensor_width_px > 0 else 0.0
    return rotation_deg, scale
