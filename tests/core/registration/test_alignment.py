from __future__ import annotations

import pytest
from astrotool_core.registration.alignment import (
    derive_alignment_guidance,
    nominal_registration,
    transform_point_a_to_b,
)
from astrotool_core.registration.geometry import polygon_centroid, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior, scale_ratio
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)

_GUIDE = OpticalPrior(name="guide", sensor_width_px=200, sensor_height_px=100,
                       pixel_scale_arcsec=3.0)


def _result(
    polygon: tuple[tuple[float, float], ...],
    status: RegistrationStatus = RegistrationStatus.OK_OVERLAP,
) -> CrossCameraRegistrationResult:
    return CrossCameraRegistrationResult(
        method=RegistrationMethod.TERRESTRIAL, status=status,
        polygon_a_in_b=polygon, overlap_polygon=polygon,
    )


class TestDeriveAlignmentGuidance:
    def test_returns_none_for_a_failed_result(self) -> None:
        failed = CrossCameraRegistrationResult(
            method=RegistrationMethod.TERRESTRIAL,
            status=RegistrationStatus.NO_VALID_REGISTRATION,
        )
        assert derive_alignment_guidance(failed, _GUIDE) is None

    def test_fully_contained_main_needs_no_adjustment(self) -> None:
        # Guide frame is [0,200]x[0,100]; a small polygon centered at the
        # same point is fully inside it.
        main_polygon = rect_polygon(20.0, 10.0, center=(100.0, 50.0))
        guidance = derive_alignment_guidance(_result(main_polygon), _GUIDE)
        assert guidance is not None
        assert guidance.fully_contained
        assert guidance.magnitude_px == 0.0

    def test_offset_main_reports_the_correct_direction(self) -> None:
        # Main's own footprint pokes outside guide's [0,200]x[0,100]
        # frame on the top-left (spans x:[-5,25], y:[-5,25]) -- not fully
        # contained, and its own centroid (10, 10) is left/up of guide's
        # center (100, 50).
        main_polygon = rect_polygon(30.0, 30.0, center=(10.0, 10.0))
        guidance = derive_alignment_guidance(_result(main_polygon), _GUIDE)
        assert guidance is not None
        assert not guidance.fully_contained
        assert guidance.direction_dx < 0.0  # left
        assert guidance.direction_dy < 0.0  # up
        assert "up" in guidance.description and "left" in guidance.description
        assert guidance.magnitude_px > 0.0

    def test_offset_to_the_right_and_down(self) -> None:
        # Pokes outside on the bottom-right (spans x:[180,210], y:[80,110]).
        main_polygon = rect_polygon(30.0, 30.0, center=(195.0, 95.0))
        guidance = derive_alignment_guidance(_result(main_polygon), _GUIDE)
        assert guidance is not None
        assert not guidance.fully_contained
        assert guidance.direction_dx > 0.0
        assert guidance.direction_dy > 0.0
        assert "down" in guidance.description and "right" in guidance.description

    def test_works_for_a_zero_overlap_result_too(self) -> None:
        """Issue #29: alignment guidance must be derivable even before
        the fields overlap at all (the star-field OK_NO_OVERLAP case)."""
        main_polygon = rect_polygon(10.0, 10.0, center=(-500.0, -500.0))
        result = _result(main_polygon, status=RegistrationStatus.OK_NO_OVERLAP)
        guidance = derive_alignment_guidance(result, _GUIDE)
        assert guidance is not None
        assert not guidance.fully_contained
        assert guidance.direction_dx < 0.0 and guidance.direction_dy < 0.0


_MAIN = OpticalPrior(name="main", sensor_width_px=40, sensor_height_px=30,
                      pixel_scale_arcsec=1.0)


class TestTransformPointAToB:
    def test_a_frame_corners_land_exactly_on_polygon_a_in_b(self) -> None:
        # rect_polygon's own corner order (TL, TR, BR, BL of the
        # *unrotated* rect) matches A's own frame corners in the same
        # order -- if the transform's derivation is right, projecting
        # each one individually must reproduce polygon_a_in_b exactly.
        scale = 2.5
        rotation_deg = 35.0
        center_b = (120.0, 80.0)
        polygon = rect_polygon(
            _MAIN.sensor_width_px * scale, _MAIN.sensor_height_px * scale,
            center=center_b, rotation_deg=rotation_deg,
        )
        result = CrossCameraRegistrationResult(
            method=RegistrationMethod.TERRESTRIAL, status=RegistrationStatus.OK_OVERLAP,
            rotation_deg=rotation_deg, scale=scale, polygon_a_in_b=polygon,
        )
        corners_a = (
            (0.0, 0.0),
            (float(_MAIN.sensor_width_px), 0.0),
            (float(_MAIN.sensor_width_px), float(_MAIN.sensor_height_px)),
            (0.0, float(_MAIN.sensor_height_px)),
        )

        for corner_a, expected_b in zip(corners_a, polygon, strict=True):
            got = transform_point_a_to_b(corner_a, _MAIN, result)
            assert got[0] == pytest.approx(expected_b[0], abs=1e-6)
            assert got[1] == pytest.approx(expected_b[1], abs=1e-6)

    def test_a_frame_center_maps_to_the_polygons_own_centroid(self) -> None:
        scale = 1.0
        polygon = rect_polygon(
            _MAIN.sensor_width_px * scale, _MAIN.sensor_height_px * scale,
            center=(0.0, 0.0), rotation_deg=0.0,
        )
        result = CrossCameraRegistrationResult(
            method=RegistrationMethod.TERRESTRIAL, status=RegistrationStatus.OK_OVERLAP,
            rotation_deg=0.0, scale=scale, polygon_a_in_b=polygon,
        )
        center_a = (_MAIN.sensor_width_px / 2.0, _MAIN.sensor_height_px / 2.0)

        got = transform_point_a_to_b(center_a, _MAIN, result)

        centroid = polygon_centroid(polygon)
        assert got[0] == pytest.approx(centroid[0], abs=1e-6)
        assert got[1] == pytest.approx(centroid[1], abs=1e-6)


class TestNominalRegistration:
    def test_reports_zero_confidence_as_an_explicitly_uncalibrated_fallback(self) -> None:
        result = nominal_registration(_MAIN, _GUIDE)
        assert result.confidence == 0.0

    def test_is_centered_unrotated_and_uses_the_known_plate_scale_ratio(self) -> None:
        result = nominal_registration(_MAIN, _GUIDE)

        assert result.rotation_deg == 0.0
        assert result.scale == pytest.approx(scale_ratio(_MAIN, _GUIDE))
        assert result.polygon_a_in_b is not None
        centroid = polygon_centroid(result.polygon_a_in_b)
        assert centroid[0] == pytest.approx(_GUIDE.sensor_width_px / 2.0)
        assert centroid[1] == pytest.approx(_GUIDE.sensor_height_px / 2.0)
