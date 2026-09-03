from __future__ import annotations

from astrotool_core.registration.alignment import derive_alignment_guidance
from astrotool_core.registration.geometry import rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior
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
