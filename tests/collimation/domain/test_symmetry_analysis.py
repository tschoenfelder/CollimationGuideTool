import numpy as np
import pytest
from astrotool_core.frames.analysis_plane import AnalysisPlane, build_analysis_plane
from astrotool_core.testing.frame_factory import donut_image, make_frame, with_shadow
from collimation_tool.domain.symmetry_analysis import detect_obstruction


def _reference_plane() -> tuple[np.ndarray, AnalysisPlane]:
    image = donut_image(
        (240, 240),
        outer_center=(120.0, 120.0),
        outer_radius=50.0,
        inner_center=(120.0, 120.0),
        inner_radius=20.0,
        peak=3000.0,
    )
    return image, build_analysis_plane(make_frame(image))


def test_detects_shadow_at_expected_angle_and_position() -> None:
    ref_image, ref_plane = _reference_plane()
    shadowed = with_shadow(ref_image, center=(160.0, 120.0), radius=10.0, depth=500.0)
    cur_plane = build_analysis_plane(make_frame(shadowed))

    result = detect_obstruction(ref_plane, cur_plane, 120.0, 120.0)

    assert result is not None
    assert result.shadow_center_x == pytest.approx(160.0, abs=0.5)
    assert result.shadow_center_y == pytest.approx(120.0, abs=0.5)
    assert result.angle_deg == pytest.approx(0.0, abs=1.0)
    assert result.shadow_area_px > 0
    assert result.confidence > 0.5


def test_shadow_above_reference_reports_negative_90_degrees() -> None:
    ref_image, ref_plane = _reference_plane()
    shadowed = with_shadow(ref_image, center=(120.0, 80.0), radius=10.0, depth=500.0)
    cur_plane = build_analysis_plane(make_frame(shadowed))

    result = detect_obstruction(ref_plane, cur_plane, 120.0, 120.0)

    assert result is not None
    assert result.angle_deg == pytest.approx(-90.0, abs=1.0)


def test_no_shadow_when_frames_are_identical() -> None:
    _, ref_plane = _reference_plane()
    result = detect_obstruction(ref_plane, ref_plane, 120.0, 120.0)
    assert result is None


def test_shadow_below_minimum_area_is_rejected() -> None:
    ref_image, ref_plane = _reference_plane()
    shadowed = with_shadow(ref_image, center=(160.0, 120.0), radius=1.0, depth=500.0)
    cur_plane = build_analysis_plane(make_frame(shadowed))

    result = detect_obstruction(ref_plane, cur_plane, 120.0, 120.0, min_shadow_px=1000)
    assert result is None
