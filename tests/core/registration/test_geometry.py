from __future__ import annotations

import math

import pytest
from astrotool_core.registration.geometry import (
    ensure_ccw,
    fully_contains,
    overlap_area,
    overlap_polygon,
    polygon_area,
    polygon_bounds,
    polygon_centroid,
    rect_polygon,
    translate_polygon,
)


class TestRectPolygon:
    def test_unrotated_rectangle_has_the_expected_corners(self) -> None:
        poly = rect_polygon(40.0, 20.0, center=(100.0, 50.0))
        assert poly[0] == pytest.approx((80.0, 40.0))  # top-left
        assert poly[2] == pytest.approx((120.0, 60.0))  # bottom-right

    def test_rotated_rectangle_area_is_unchanged(self) -> None:
        poly = rect_polygon(40.0, 20.0, center=(0.0, 0.0), rotation_deg=37.0)
        assert polygon_area(poly) == pytest.approx(800.0)

    def test_a_360_degree_rotation_returns_to_the_same_shape(self) -> None:
        poly0 = rect_polygon(30.0, 10.0, center=(5.0, 5.0), rotation_deg=0.0)
        poly360 = rect_polygon(30.0, 10.0, center=(5.0, 5.0), rotation_deg=360.0)
        for p0, p360 in zip(poly0, poly360, strict=True):
            assert p0 == pytest.approx(p360)


class TestPolygonArea:
    def test_area_of_a_simple_square(self) -> None:
        square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
        assert polygon_area(square) == pytest.approx(100.0)

    def test_area_is_winding_order_independent(self) -> None:
        cw = ((0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0))
        assert polygon_area(cw) == pytest.approx(100.0)

    def test_degenerate_polygon_has_zero_area(self) -> None:
        assert polygon_area(((0.0, 0.0), (1.0, 1.0))) == 0.0
        assert polygon_area(()) == 0.0


class TestPolygonCentroid:
    def test_centroid_of_a_square(self) -> None:
        square = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
        assert polygon_centroid(square) == pytest.approx((5.0, 5.0))

    def test_raises_for_an_empty_polygon(self) -> None:
        with pytest.raises(ValueError):
            polygon_centroid(())


class TestOverlap:
    def test_identical_rectangles_overlap_completely(self) -> None:
        a = rect_polygon(20.0, 10.0, center=(0.0, 0.0))
        b = rect_polygon(20.0, 10.0, center=(0.0, 0.0))
        assert overlap_area(a, b) == pytest.approx(200.0)
        assert fully_contains(b, a)

    def test_disjoint_rectangles_have_no_overlap(self) -> None:
        a = rect_polygon(10.0, 10.0, center=(0.0, 0.0))
        b = rect_polygon(10.0, 10.0, center=(100.0, 100.0))
        assert overlap_polygon(a, b) == ()
        assert overlap_area(a, b) == 0.0
        assert not fully_contains(b, a)

    def test_partial_overlap_area_is_correct(self) -> None:
        a = rect_polygon(10.0, 10.0, center=(0.0, 0.0))  # [-5,5]x[-5,5]
        b = rect_polygon(10.0, 10.0, center=(5.0, 0.0))  # [0,10]x[-5,5]
        # Overlap: x in [0,5], y in [-5,5] -> 5x10 = 50
        assert overlap_area(a, b) == pytest.approx(50.0)

    def test_small_rectangle_fully_inside_a_larger_one(self) -> None:
        outer = rect_polygon(100.0, 100.0, center=(0.0, 0.0))
        inner = rect_polygon(10.0, 10.0, center=(3.0, -2.0))
        assert fully_contains(outer, inner)
        assert overlap_area(outer, inner) == pytest.approx(polygon_area(inner))

    def test_a_rotated_rectangle_partially_overlapping_an_axis_aligned_one(self) -> None:
        # A 90-degree rotation of a non-square rectangle swaps width/height
        # -- area must match the swapped-dimension rectangle exactly.
        a = rect_polygon(40.0, 20.0, center=(0.0, 0.0), rotation_deg=90.0)
        b = rect_polygon(20.0, 40.0, center=(0.0, 0.0), rotation_deg=0.0)
        assert overlap_area(a, b) == pytest.approx(800.0, abs=1e-6)

    def test_oag_like_geometry_adjacent_but_never_overlapping(self) -> None:
        """Issue #29 #9: an OAG's field can be close to Main but
        intentionally outside it -- must resolve as a real, valid
        zero-overlap geometry, not a degenerate/undefined one."""
        main = rect_polygon(50.0, 50.0, center=(0.0, 0.0))
        oag = rect_polygon(30.0, 30.0, center=(50.0, 0.0))  # touches main's right edge, no overlap
        assert overlap_polygon(main, oag) == ()

    def test_ensure_ccw_is_idempotent(self) -> None:
        poly = rect_polygon(10.0, 10.0)
        assert ensure_ccw(poly) == ensure_ccw(ensure_ccw(poly))

    def test_ensure_ccw_preserves_area(self) -> None:
        cw = ((0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0))
        assert polygon_area(ensure_ccw(cw)) == pytest.approx(polygon_area(cw))

    def test_a_polygon_with_either_winding_overlaps_correctly_once_normalized(self) -> None:
        """Overlap logic only works when both inputs are consistently
        wound -- an externally-built polygon (e.g. WCS-projected sky
        corners, whose winding this module doesn't control) must overlap
        correctly with a `rect_polygon` output regardless of which way
        it happened to wind."""
        cw = ((0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0))
        ccw = tuple(reversed(cw))
        square = rect_polygon(10.0, 10.0)  # centered at (0,0), spans [-5,5]
        # cw/ccw span [0,10]x[0,10] -- overlaps [0,5]x[0,5] with square.
        assert overlap_area(cw, square) == pytest.approx(25.0)
        assert overlap_area(ccw, square) == pytest.approx(25.0)


class TestPolygonBounds:
    def test_bounds_of_a_rotated_rectangle(self) -> None:
        poly = rect_polygon(10.0, 10.0, center=(0.0, 0.0), rotation_deg=45.0)
        min_x, min_y, max_x, max_y = polygon_bounds(poly)
        expected_half_diagonal = 10.0 / math.sqrt(2.0)
        assert min_x == pytest.approx(-expected_half_diagonal)
        assert max_x == pytest.approx(expected_half_diagonal)


class TestTranslatePolygon:
    def test_shifts_every_vertex(self) -> None:
        poly = rect_polygon(10.0, 10.0, center=(0.0, 0.0))
        shifted = translate_polygon(poly, 5.0, -3.0)
        assert shifted[0] == pytest.approx((poly[0][0] + 5.0, poly[0][1] - 3.0))
