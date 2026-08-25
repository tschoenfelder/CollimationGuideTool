import pytest
from astrotool_core.target.detector import detect_sources
from astrotool_core.testing.frame_factory import StarSpec, single_star_image, star_field_image


def test_single_gaussian_star_round_trips_to_a_point_source_within_tolerance() -> None:
    image = single_star_image((120, 120), x=60.3, y=45.7, peak=2000.0, sigma=2.5, background=100.0)

    result = detect_sources(image)

    assert len(result.sources) == 1
    source = result.sources[0]
    assert source.x == pytest.approx(60.3, abs=0.3)
    assert source.y == pytest.approx(45.7, abs=0.3)
    assert source.kind == "normal_star"
    assert source.saturated is False
    assert source.donut_like is False


def test_multiple_stars_are_all_detected() -> None:
    stars = [
        StarSpec(x=30.0, y=30.0, peak=3000.0, sigma=2.5),
        StarSpec(x=90.0, y=80.0, peak=5000.0, sigma=2.5),
    ]
    image = star_field_image((120, 120), stars, background=100.0)

    result = detect_sources(image)

    assert len(result.sources) == 2
    xs = sorted(source.x for source in result.sources)
    assert xs[0] == pytest.approx(30.0, abs=0.3)
    assert xs[1] == pytest.approx(90.0, abs=0.3)


def test_empty_dark_field_detects_nothing_and_flags_too_dark() -> None:
    image = star_field_image((60, 60), [], background=50.0)

    result = detect_sources(image, exposure_s=1.0)

    assert result.sources == ()
    assert result.image_quality == "too_dark"
