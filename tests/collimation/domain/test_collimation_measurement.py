import numpy as np
import pytest
from astrotool_core.frames.analysis_plane import AnalysisPlane, build_analysis_plane
from astrotool_core.testing.frame_factory import donut_image, make_frame
from collimation_tool.domain.collimation_measurement import (
    DonutAnalyzer,
    Point2D,
    ReferenceCenterCalibration,
    auto_stretch,
    compare_circle_centers,
    detect_clipping,
    estimate_background,
    extract_edge_points,
    fit_circle,
    fit_ellipse,
    peak_location,
    saturation_fraction,
)


class TestPoint2D:
    def test_distance_to(self) -> None:
        assert Point2D(0.0, 0.0).distance_to(Point2D(3.0, 4.0)) == pytest.approx(5.0)

    def test_subtraction(self) -> None:
        assert Point2D(5.0, 5.0) - Point2D(2.0, 1.0) == Point2D(3.0, 4.0)


class TestReferenceCenterCalibration:
    def test_frame_center_default(self) -> None:
        cal = ReferenceCenterCalibration()
        assert cal.compute(640, 480) == Point2D(320.0, 240.0)
        assert cal.is_calibrated is False

    def test_calibrated_offset(self) -> None:
        cal = ReferenceCenterCalibration(offset_x_px=5.0, offset_y_px=-3.0, source="calibrated")
        assert cal.compute(640, 480) == Point2D(325.0, 237.0)
        assert cal.is_calibrated is True
        assert cal.has_offset is True


class TestStretchUtilities:
    def test_estimate_background_uniform_field(self) -> None:
        data = np.full((32, 32), 100.0, dtype=np.float32)
        bg, sigma = estimate_background(data)
        assert bg == pytest.approx(100.0)
        assert sigma == 1.0  # floor when sigma == 0

    def test_auto_stretch_maps_to_full_uint8_range(self) -> None:
        data = np.linspace(0, 1000, 100, dtype=np.float32).reshape(10, 10)
        stretched = auto_stretch(data, low_percentile=0.0, high_percentile=100.0)
        assert stretched.dtype == np.uint8
        assert stretched.min() == 0
        assert stretched.max() == 255

    def test_saturation_fraction(self) -> None:
        data = np.zeros((10, 10), dtype=np.float32)
        data[0, :5] = 65535.0  # 5 of 100 pixels saturated at 16-bit
        assert saturation_fraction(data, bit_depth=16) == pytest.approx(0.05)

    def test_peak_location(self) -> None:
        data = np.zeros((10, 10), dtype=np.float32)
        data[3, 7] = 999.0
        col, row, value = peak_location(data)
        assert (col, row, value) == (7.0, 3.0, 999.0)


class TestFitCircle:
    def test_fits_a_perfect_circle(self) -> None:
        theta = np.linspace(0, 2 * np.pi, 64, endpoint=False)
        points = np.column_stack([50.0 + 20.0 * np.cos(theta), 50.0 + 20.0 * np.sin(theta)])
        fit = fit_circle(points)
        assert fit.center_x == pytest.approx(50.0, abs=0.1)
        assert fit.center_y == pytest.approx(50.0, abs=0.1)
        assert fit.radius_x == pytest.approx(20.0, abs=0.1)
        assert fit.confidence > 0.99

    def test_degenerate_with_too_few_points(self) -> None:
        fit = fit_circle(np.array([[0.0, 0.0], [1.0, 1.0]]))
        assert fit.confidence == 0.0
        assert fit.radius_x == 0.0


class TestFitEllipse:
    def test_fits_a_genuine_ellipse(self) -> None:
        theta = np.linspace(0, 2 * np.pi, 64, endpoint=False)
        points = np.column_stack([50.0 + 30.0 * np.cos(theta), 60.0 + 15.0 * np.sin(theta)])
        fit = fit_ellipse(points)
        assert fit.center_x == pytest.approx(50.0, abs=0.1)
        assert fit.center_y == pytest.approx(60.0, abs=0.1)
        assert fit.radius_x == pytest.approx(30.0, abs=0.1)
        assert fit.radius_y == pytest.approx(15.0, abs=0.1)
        assert fit.confidence > 0.99
        assert fit.is_circle is False

    def test_falls_back_to_fit_circle_with_too_few_points(self) -> None:
        theta = np.linspace(0, 2 * np.pi, 4, endpoint=False)
        points = np.column_stack([50.0 + 20.0 * np.cos(theta), 50.0 + 20.0 * np.sin(theta)])
        fit = fit_ellipse(points)
        assert fit.radius_x == pytest.approx(fit.radius_y)

    def test_circular_points_yield_a_near_circular_ellipse(self) -> None:
        theta = np.linspace(0, 2 * np.pi, 64, endpoint=False)
        points = np.column_stack([50.0 + 20.0 * np.cos(theta), 50.0 + 20.0 * np.sin(theta)])
        fit = fit_ellipse(points)
        assert fit.is_circle is True


class TestExtractEdgePoints:
    def test_empty_mask_returns_empty_array(self) -> None:
        mask = np.zeros((10, 10), dtype=bool)
        points = extract_edge_points(mask)
        assert points.shape == (0, 2)

    def test_solid_square_yields_boundary_only(self) -> None:
        mask = np.zeros((10, 10), dtype=bool)
        mask[2:8, 2:8] = True  # 6x6 solid block
        points = extract_edge_points(mask)
        # interior 4x4 excluded, so edge count == 36 - 16 == 20
        assert len(points) == 20


class TestDetectClipping:
    def test_not_clipped_when_comfortably_inside(self) -> None:
        fit = fit_circle(
            np.column_stack(
                [
                    50.0 + 20.0 * np.cos(np.linspace(0, 2 * np.pi, 32)),
                    50.0 + 20.0 * np.sin(np.linspace(0, 2 * np.pi, 32)),
                ]
            )
        )
        assert detect_clipping(fit, frame_width=200, frame_height=200) is False

    def test_clipped_when_touching_edge(self) -> None:
        fit = fit_circle(
            np.column_stack(
                [
                    5.0 + 20.0 * np.cos(np.linspace(0, 2 * np.pi, 32)),
                    50.0 + 20.0 * np.sin(np.linspace(0, 2 * np.pi, 32)),
                ]
            )
        )
        assert detect_clipping(fit, frame_width=200, frame_height=200) is True


def test_compare_circle_centers() -> None:
    theta = np.linspace(0, 2 * np.pi, 16)
    a = fit_circle(np.column_stack([10.0 + np.cos(theta), 10.0 + np.sin(theta)]))
    b = fit_circle(np.column_stack([13.0 + np.cos(theta), 14.0 + np.sin(theta)]))
    assert compare_circle_centers(a, b) == pytest.approx(5.0, abs=0.1)


class TestDonutAnalyzer:
    def _plane(
        self,
        *,
        outer_center: tuple[float, float],
        outer_radius: float,
        inner_center: tuple[float, float],
        inner_radius: float,
        peak: float,
    ) -> AnalysisPlane:
        image = donut_image(
            (240, 240),
            outer_center=outer_center,
            outer_radius=outer_radius,
            inner_center=inner_center,
            inner_radius=inner_radius,
            peak=peak,
        )
        return build_analysis_plane(make_frame(image))

    def test_well_collimated_donut_has_small_error(self) -> None:
        plane = self._plane(
            outer_center=(120.0, 120.0),
            outer_radius=50.0,
            inner_center=(120.0, 120.0),
            inner_radius=20.0,
            peak=3000.0,
        )
        result = DonutAnalyzer().analyze(plane)
        assert result.reason == "ok"
        assert result.measurement is not None
        assert result.measurement.error_magnitude_px == pytest.approx(0.0, abs=0.5)
        assert result.measurement.is_collimated is True

    def test_miscollimated_donut_reports_offset_error_vector(self) -> None:
        plane = self._plane(
            outer_center=(120.0, 120.0),
            outer_radius=50.0,
            inner_center=(125.0, 118.0),
            inner_radius=20.0,
            peak=3000.0,
        )
        result = DonutAnalyzer().analyze(plane)
        assert result.reason == "ok"
        measurement = result.measurement
        assert measurement is not None
        assert measurement.error_x_px == pytest.approx(5.0, abs=0.5)
        assert measurement.error_y_px == pytest.approx(-2.0, abs=0.5)
        assert measurement.is_collimated is False

    def test_dark_frame_has_no_signal(self) -> None:
        plane = build_analysis_plane(make_frame(np.full((64, 64), 100.0, dtype=np.float32)))
        result = DonutAnalyzer().analyze(plane)
        assert result.reason == "no_signal"
        assert result.measurement is None

    def test_donut_clipped_by_frame_edge(self) -> None:
        plane = self._plane(
            outer_center=(20.0, 120.0),
            outer_radius=50.0,
            inner_center=(20.0, 120.0),
            inner_radius=20.0,
            peak=3000.0,
        )
        result = DonutAnalyzer().analyze(plane)
        assert result.reason == "clipped"
