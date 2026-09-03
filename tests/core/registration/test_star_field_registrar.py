"""Tests for StarFieldRegistrar/geometry_from_wcs — every WCS here is
synthetic (built directly via astropy.wcs.WCS), never solved by a real
ASTAP process, per issue #29's own "ASTAP adapter tests should be
separable from pure WCS/geometry tests so ordinary CI need not require
ASTAP unless explicitly configured."
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from astropy.wcs import WCS
from astrotool_core.registration.astap_adapter import (
    AstapSolveHint,
    AstapSolveResult,
    AstapSolveStatus,
)
from astrotool_core.registration.geometry import fully_contains, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import RegistrationMethod, RegistrationStatus
from astrotool_core.registration.star_field_registrar import (
    StarFieldHints,
    StarFieldRegistrar,
    geometry_from_wcs,
)


def _make_wcs(
    *,
    crval_deg: tuple[float, float],
    crpix: tuple[float, float],
    pixel_scale_deg: float,
    rotation_deg: float = 0.0,
) -> WCS:
    wcs = WCS(naxis=2)
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.crval = list(crval_deg)
    wcs.wcs.crpix = list(crpix)
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    # East-left convention (RA decreases as pixel x increases) -- the
    # usual real-sky orientation; consistent across every WCS this test
    # file builds, which is all that matters for the *relative* geometry
    # under test here.
    wcs.wcs.cd = [
        [-pixel_scale_deg * cos_t, pixel_scale_deg * sin_t],
        [pixel_scale_deg * sin_t, pixel_scale_deg * cos_t],
    ]
    return wcs


_MAIN = OpticalPrior(name="main", sensor_width_px=40, sensor_height_px=30,
                      pixel_scale_arcsec=1.0)
_GUIDE = OpticalPrior(name="guide", sensor_width_px=400, sensor_height_px=300,
                       pixel_scale_arcsec=1.0)
_CENTER = (150.0, 20.0)  # an arbitrary (ra_deg, dec_deg) sky pointing


class TestGeometryFromWcs:
    def test_identical_pointing_and_scale_is_fully_contained(self) -> None:
        # Both cameras point at exactly the same sky center, own-frame-
        # centered, same pixel scale, no relative rotation -- main's
        # smaller frame should land centered and fully inside guide's.
        scale_deg = 0.0005
        wcs_a = _make_wcs(
            crval_deg=_CENTER, crpix=(_MAIN.sensor_width_px / 2, _MAIN.sensor_height_px / 2),
            pixel_scale_deg=scale_deg,
        )
        wcs_b = _make_wcs(
            crval_deg=_CENTER, crpix=(_GUIDE.sensor_width_px / 2, _GUIDE.sensor_height_px / 2),
            pixel_scale_deg=scale_deg,
        )

        result = geometry_from_wcs(wcs_a, _MAIN, wcs_b, _GUIDE)

        assert result.status is RegistrationStatus.OK_OVERLAP
        assert result.method is RegistrationMethod.STAR_FIELD
        assert result.ok
        assert result.polygon_a_in_b is not None
        guide_rect = rect_polygon(
            _GUIDE.sensor_width_px, _GUIDE.sensor_height_px,
            center=(_GUIDE.sensor_width_px / 2.0, _GUIDE.sensor_height_px / 2.0),
        )
        assert fully_contains(guide_rect, result.polygon_a_in_b)
        assert result.rotation_deg == pytest.approx(0.0, abs=1.0)

    def test_far_apart_pointings_report_ok_no_overlap_not_failure(self) -> None:
        """Issue #29 #4: two successfully-solved frames with zero pixel
        overlap is real, valid geometry -- not a failure status."""
        scale_deg = 0.0005
        wcs_a = _make_wcs(
            crval_deg=_CENTER, crpix=(_MAIN.sensor_width_px / 2, _MAIN.sensor_height_px / 2),
            pixel_scale_deg=scale_deg,
        )
        # 30 degrees away on the sky -- nowhere near B's own small field.
        wcs_b = _make_wcs(
            crval_deg=(_CENTER[0] + 30.0, _CENTER[1]),
            crpix=(_GUIDE.sensor_width_px / 2, _GUIDE.sensor_height_px / 2),
            pixel_scale_deg=scale_deg,
        )

        result = geometry_from_wcs(wcs_a, _MAIN, wcs_b, _GUIDE)

        assert result.status is RegistrationStatus.OK_NO_OVERLAP
        assert result.ok  # still a *valid* result, not a failure
        assert result.overlap_polygon == ()
        assert result.polygon_a_in_b is not None  # geometry still reported

    def test_recovers_a_known_relative_rotation(self) -> None:
        scale_deg = 0.0005
        wcs_a = _make_wcs(
            crval_deg=_CENTER, crpix=(_MAIN.sensor_width_px / 2, _MAIN.sensor_height_px / 2),
            pixel_scale_deg=scale_deg,
        )
        wcs_b = _make_wcs(
            crval_deg=_CENTER, crpix=(_GUIDE.sensor_width_px / 2, _GUIDE.sensor_height_px / 2),
            pixel_scale_deg=scale_deg, rotation_deg=40.0,
        )

        result = geometry_from_wcs(wcs_a, _MAIN, wcs_b, _GUIDE)

        assert result.rotation_deg is not None
        # B is rotated +40 degrees relative to A -- A's own content
        # appears rotated by the opposite sense within B's own frame.
        assert abs(result.rotation_deg) == pytest.approx(40.0, abs=1.0)

    def test_recovers_a_known_scale_ratio(self) -> None:
        # Guide's pixel scale is twice as coarse as main's -- one main
        # pixel covers 2 guide-pixels' worth of sky.
        main_scale_deg = 0.0005
        guide_scale_deg = 0.001
        wcs_a = _make_wcs(
            crval_deg=_CENTER, crpix=(_MAIN.sensor_width_px / 2, _MAIN.sensor_height_px / 2),
            pixel_scale_deg=main_scale_deg,
        )
        wcs_b = _make_wcs(
            crval_deg=_CENTER, crpix=(_GUIDE.sensor_width_px / 2, _GUIDE.sensor_height_px / 2),
            pixel_scale_deg=guide_scale_deg,
        )

        result = geometry_from_wcs(wcs_a, _MAIN, wcs_b, _GUIDE)

        assert result.scale is not None
        assert result.scale == pytest.approx(main_scale_deg / guide_scale_deg, rel=0.02)

    def test_supports_oag_like_geometry_adjacent_but_never_overlapping(self) -> None:
        """Issue #29 #9: OAG registration is star-field-only, but the
        common geometry model must still represent it correctly (a real,
        valid zero-overlap result, same as the far-apart-pointings case)."""
        scale_deg = 0.0005
        oag_prior = OpticalPrior(name="oag", sensor_width_px=30, sensor_height_px=30,
                                  pixel_scale_arcsec=1.0)
        wcs_main = _make_wcs(
            crval_deg=_CENTER, crpix=(_MAIN.sensor_width_px / 2, _MAIN.sensor_height_px / 2),
            pixel_scale_deg=scale_deg,
        )
        # OAG's own field sits just outside main's -- a real, supported,
        # non-containment geometry (not "smaller frame inside larger").
        oag_center = (_CENTER[0] + 0.05, _CENTER[1])
        oag_crpix = (oag_prior.sensor_width_px / 2, oag_prior.sensor_height_px / 2)
        wcs_oag = _make_wcs(crval_deg=oag_center, crpix=oag_crpix, pixel_scale_deg=scale_deg)

        result = geometry_from_wcs(wcs_main, _MAIN, wcs_oag, oag_prior)

        assert result.ok  # solved geometry either way, overlap or not
        assert result.polygon_a_in_b is not None


class _FakeSolver:
    def __init__(self, results: dict[str, AstapSolveResult]) -> None:
        self._results = results
        self.calls: list[tuple[Path, AstapSolveHint | None]] = []

    def solve(self, fits_path: Path, *, hint: AstapSolveHint | None = None) -> AstapSolveResult:
        self.calls.append((fits_path, hint))
        return self._results[fits_path.name]


def _solved(wcs: WCS) -> AstapSolveResult:
    return AstapSolveResult(AstapSolveStatus.SOLVED, wcs=wcs)


def _failed(status: AstapSolveStatus, message: str = "") -> AstapSolveResult:
    return AstapSolveResult(status, message=message)


class TestStarFieldRegistrar:
    def test_both_frames_solved_reports_geometry(self) -> None:
        scale_deg = 0.0005
        wcs_a = _make_wcs(
            crval_deg=_CENTER, crpix=(_MAIN.sensor_width_px / 2, _MAIN.sensor_height_px / 2),
            pixel_scale_deg=scale_deg,
        )
        wcs_b = _make_wcs(
            crval_deg=_CENTER, crpix=(_GUIDE.sensor_width_px / 2, _GUIDE.sensor_height_px / 2),
            pixel_scale_deg=scale_deg,
        )
        solver = _FakeSolver({"a.fits": _solved(wcs_a), "b.fits": _solved(wcs_b)})
        registrar = StarFieldRegistrar(solver)

        result = registrar.register(Path("a.fits"), Path("b.fits"), _MAIN, _GUIDE)

        assert result.status is RegistrationStatus.OK_OVERLAP
        assert len(solver.calls) == 2

    def test_astap_unavailable_short_circuits(self) -> None:
        solver = _FakeSolver({
            "a.fits": _failed(AstapSolveStatus.ASTAP_UNAVAILABLE, "not found"),
            "b.fits": _solved(_make_wcs(crval_deg=_CENTER, crpix=(1, 1), pixel_scale_deg=0.001)),
        })
        registrar = StarFieldRegistrar(solver)

        result = registrar.register(Path("a.fits"), Path("b.fits"), _MAIN, _GUIDE)

        assert result.status is RegistrationStatus.ASTAP_UNAVAILABLE
        assert not result.ok
        # Never bothered solving B once A's own solve reported unavailable.
        assert len(solver.calls) == 1

    def test_frame_a_solve_failure_is_reported_distinctly(self) -> None:
        solver = _FakeSolver({
            "a.fits": _failed(AstapSolveStatus.SOLVE_FAILED, "insufficient stars"),
            "b.fits": _solved(_make_wcs(crval_deg=_CENTER, crpix=(1, 1), pixel_scale_deg=0.001)),
        })
        registrar = StarFieldRegistrar(solver)

        result = registrar.register(Path("a.fits"), Path("b.fits"), _MAIN, _GUIDE)

        assert result.status is RegistrationStatus.SOLVE_FAILED_A
        assert len(solver.calls) == 1  # B is never attempted once A fails

    def test_frame_b_solve_failure_is_reported_distinctly(self) -> None:
        wcs_a = _make_wcs(crval_deg=_CENTER, crpix=(1, 1), pixel_scale_deg=0.001)
        solver = _FakeSolver({
            "a.fits": _solved(wcs_a),
            "b.fits": _failed(AstapSolveStatus.SOLVE_FAILED, "insufficient stars"),
        })
        registrar = StarFieldRegistrar(solver)

        result = registrar.register(Path("a.fits"), Path("b.fits"), _MAIN, _GUIDE)

        assert result.status is RegistrationStatus.SOLVE_FAILED_B
        assert len(solver.calls) == 2  # A did solve -- B was still attempted

    def test_hints_are_forwarded_per_frame(self) -> None:
        wcs = _make_wcs(crval_deg=_CENTER, crpix=(1, 1), pixel_scale_deg=0.001)
        solver = _FakeSolver({"a.fits": _solved(wcs), "b.fits": _solved(wcs)})
        registrar = StarFieldRegistrar(solver)
        hint_a = AstapSolveHint(ra_deg=10.0)
        hint_b = AstapSolveHint(ra_deg=20.0)

        registrar.register(
            Path("a.fits"), Path("b.fits"), _MAIN, _GUIDE,
            hints=StarFieldHints(hint_a=hint_a, hint_b=hint_b),
        )

        assert solver.calls[0] == (Path("a.fits"), hint_a)
        assert solver.calls[1] == (Path("b.fits"), hint_b)
