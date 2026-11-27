"""Tests for ArtificialStarRegistrar — issue #37: register Main and
Guide from a single isolated point source (e.g. a pinhole/fiber
artificial star), with no ASTAP dependency and no rich terrestrial
texture required. Rotation is never measurable from one point --
`assumed_rotation_deg` is an explicit, documented prior (default 0.0,
cameras assumed mechanically co-aligned), not something this registrar
derives."""

from __future__ import annotations

import math

import numpy as np
import pytest
from astrotool_core.registration.alignment import transform_point_a_to_b
from astrotool_core.registration.artificial_star_registrar import (
    _EDGE_MARGIN_PX,
    ArtificialStarRegistrar,
)
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import RegistrationMethod, RegistrationStatus
from astrotool_core.testing.frame_factory import StarSpec, single_star_image, star_field_image

_registrar = ArtificialStarRegistrar()

_MAIN_SHAPE = (120, 120)
_GUIDE_SHAPE = (400, 400)
_MAIN = OpticalPrior(
    name="main", sensor_width_px=120, sensor_height_px=120, pixel_scale_arcsec=1.0
)
_GUIDE = OpticalPrior(
    name="guide", sensor_width_px=400, sensor_height_px=400, pixel_scale_arcsec=1.0
)


def _blank(shape: tuple[int, int]) -> np.ndarray:
    return np.full(shape, 100.0)


class TestSinglePointMatch:
    def test_a_single_shared_point_source_is_matched_and_registered(self) -> None:
        frame_a = single_star_image(_MAIN_SHAPE, x=60.0, y=60.0, peak=5000.0, sigma=2.0)
        frame_b = single_star_image(_GUIDE_SHAPE, x=220.0, y=190.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert result.ok
        assert result.status is RegistrationStatus.OK_OVERLAP
        assert result.method is RegistrationMethod.ARTIFICIAL_STAR
        assert result.rotation_deg == 0.0
        assert result.scale == pytest.approx(1.0)
        matched = result.diagnostics["matched_point"]
        assert matched == pytest.approx((220.0, 190.0), abs=1.0)

    def test_the_recovered_transform_maps_the_reference_point_onto_the_matched_point(
        self,
    ) -> None:
        frame_a = single_star_image(_MAIN_SHAPE, x=60.0, y=60.0, peak=5000.0, sigma=2.0)
        frame_b = single_star_image(_GUIDE_SHAPE, x=250.0, y=160.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert result.ok
        projected = transform_point_a_to_b((60.0, 60.0), _MAIN, result)
        assert projected == pytest.approx((250.0, 160.0), abs=1.0)


class TestAssumedRotationPrior:
    def test_a_nonzero_assumed_rotation_is_reflected_unmeasured_in_the_result(self) -> None:
        # Star off-center in Main so rotation actually changes the
        # predicted search anchor -- a star exactly at Main's own center
        # would look identical for any rotation.
        frame_a = single_star_image(_MAIN_SHAPE, x=90.0, y=60.0, peak=5000.0, sigma=2.0)
        # Predicted (rotation=30deg, scale=1.0): center_b + rotate((30, 0), 30deg)
        angle = math.radians(30.0)
        predicted_x = 200.0 + 30.0 * math.cos(angle)
        predicted_y = 200.0 + 30.0 * math.sin(angle)
        frame_b = single_star_image(
            _GUIDE_SHAPE, x=predicted_x + 2.0, y=predicted_y - 2.0, peak=5000.0, sigma=2.0
        )

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE, assumed_rotation_deg=30.0)

        assert result.ok
        assert result.rotation_deg == 30.0  # pass-through prior, never measured


class TestMultipleGuideCandidates:
    def test_only_the_candidate_near_the_predicted_position_is_matched(self) -> None:
        frame_a = single_star_image(_MAIN_SHAPE, x=60.0, y=60.0, peak=5000.0, sigma=2.0)
        # Predicted point is guide-center (200, 200) -- one candidate close
        # to it, one far outside the match tolerance.
        frame_b = star_field_image(
            _GUIDE_SHAPE,
            [
                StarSpec(x=210.0, y=205.0, peak=5000.0, sigma=2.0),
                StarSpec(x=370.0, y=370.0, peak=5000.0, sigma=2.0),
            ],
        )

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert result.ok
        assert result.diagnostics["matched_point"] == pytest.approx((210.0, 205.0), abs=1.0)

    def test_multiple_equally_plausible_candidates_are_reported_ambiguous(self) -> None:
        frame_a = single_star_image(_MAIN_SHAPE, x=60.0, y=60.0, peak=5000.0, sigma=2.0)
        frame_b = star_field_image(
            _GUIDE_SHAPE,
            [
                StarSpec(x=210.0, y=205.0, peak=5000.0, sigma=2.0),
                StarSpec(x=190.0, y=215.0, peak=5000.0, sigma=2.0),
            ],
        )

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert not result.ok
        assert result.status is RegistrationStatus.AMBIGUOUS_MATCH
        assert result.method is RegistrationMethod.ARTIFICIAL_STAR


class TestDifferentPixelScales:
    def test_scale_ratio_from_priors_is_applied_not_searched(self) -> None:
        main = OpticalPrior(name="main", sensor_width_px=100, sensor_height_px=100,
                             pixel_scale_arcsec=0.5)
        guide = OpticalPrior(name="guide", sensor_width_px=300, sensor_height_px=300,
                              pixel_scale_arcsec=1.0)
        frame_a = single_star_image((100, 100), x=50.0, y=50.0, peak=5000.0, sigma=2.0)
        frame_b = single_star_image((300, 300), x=160.0, y=140.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, main, guide)

        assert result.ok
        assert result.scale == pytest.approx(0.5)


class TestEdgeCase:
    def test_a_source_near_but_not_clipped_by_the_edge_is_still_accepted(self) -> None:
        frame_a = single_star_image(
            _MAIN_SHAPE, x=_EDGE_MARGIN_PX + 5.0, y=60.0, peak=5000.0, sigma=2.0
        )
        frame_b = single_star_image(_GUIDE_SHAPE, x=200.0, y=200.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert result.ok


class TestEdgeAndSaturation:
    def test_a_star_cut_by_the_frame_edge_is_rejected(self) -> None:
        frame_a = single_star_image(_MAIN_SHAPE, x=1.0, y=60.0, peak=5000.0, sigma=2.0)
        frame_b = single_star_image(_GUIDE_SHAPE, x=200.0, y=200.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert result.status is RegistrationStatus.INSUFFICIENT_STARS

    def test_a_clipped_artificial_star_is_still_located(self) -> None:
        frame_a = np.minimum(
            single_star_image(_MAIN_SHAPE, x=60.0, y=60.0, peak=200000.0, sigma=4.0), 65535.0
        )
        frame_b = single_star_image(_GUIDE_SHAPE, x=220.0, y=190.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert result.ok, (result.status, result.diagnostics)
        assert result.diagnostics["reference_point"] == pytest.approx((60.0, 60.0), abs=1.0)


class TestNoUsableReferenceSource:
    def test_a_blank_main_frame_reports_insufficient_stars(self) -> None:
        frame_a = _blank(_MAIN_SHAPE)
        frame_b = single_star_image(_GUIDE_SHAPE, x=200.0, y=200.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert not result.ok
        assert result.status is RegistrationStatus.INSUFFICIENT_STARS

    def test_multiple_main_sources_also_report_insufficient_stars(self) -> None:
        frame_a = star_field_image(
            _MAIN_SHAPE,
            [
                StarSpec(x=40.0, y=40.0, peak=5000.0, sigma=2.0),
                StarSpec(x=80.0, y=80.0, peak=5000.0, sigma=2.0),
            ],
        )
        frame_b = single_star_image(_GUIDE_SHAPE, x=200.0, y=200.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert not result.ok
        assert result.status is RegistrationStatus.INSUFFICIENT_STARS


class TestOnlyOneGuideSource:
    def test_the_only_guide_source_is_the_star_even_far_from_the_prediction(self) -> None:
        frame_a = single_star_image(_MAIN_SHAPE, x=60.0, y=60.0, peak=5000.0, sigma=2.0)
        frame_b = single_star_image(_GUIDE_SHAPE, x=370.0, y=370.0, peak=5000.0, sigma=2.0)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert result.ok, (result.status, result.diagnostics)
        assert result.diagnostics["matched_point"] == pytest.approx((370.0, 370.0), abs=1.0)


class TestNoCorrespondingGuideSource:
    def test_a_blank_guide_frame_reports_no_valid_registration(self) -> None:
        frame_a = single_star_image(_MAIN_SHAPE, x=60.0, y=60.0, peak=5000.0, sigma=2.0)
        frame_b = _blank(_GUIDE_SHAPE)

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert not result.ok
        assert result.status is RegistrationStatus.NO_VALID_REGISTRATION

    def test_several_guide_sources_all_far_from_the_prediction_are_not_matched(
        self,
    ) -> None:
        frame_a = single_star_image(_MAIN_SHAPE, x=60.0, y=60.0, peak=5000.0, sigma=2.0)
        # Predicted point is guide-center (200, 200); this is well beyond
        # the tolerance window.
        frame_b = star_field_image(
            _GUIDE_SHAPE,
            [
                StarSpec(x=370.0, y=370.0, peak=5000.0, sigma=2.0),
                StarSpec(x=370.0, y=30.0, peak=5000.0, sigma=2.0),
            ],
        )

        result = _registrar.register(frame_a, frame_b, _MAIN, _GUIDE)

        assert not result.ok
        assert result.status is RegistrationStatus.NO_VALID_REGISTRATION


class TestInsufficientPrior:
    def test_a_main_fov_not_smaller_than_guide_reports_insufficient_prior(self) -> None:
        # Priors configured so Main's own FOV isn't narrower than Guide's
        # -- violates this mode's whole smaller-FOV/larger-FOV premise,
        # regardless of frame content.
        main = OpticalPrior(name="main", sensor_width_px=400, sensor_height_px=400,
                             pixel_scale_arcsec=1.0)
        guide = OpticalPrior(name="guide", sensor_width_px=120, sensor_height_px=120,
                              pixel_scale_arcsec=1.0)

        result = _registrar.register(_blank((10, 10)), _blank((10, 10)), main, guide)

        assert not result.ok
        assert result.status is RegistrationStatus.INSUFFICIENT_PRIOR


class TestNoAstapDependency:
    def test_the_module_imports_no_astap_adapter_symbol(self) -> None:
        import astrotool_core.registration.artificial_star_registrar as module

        assert not hasattr(module, "AstapSolver")
        assert not hasattr(module, "AstapCliSolver")
