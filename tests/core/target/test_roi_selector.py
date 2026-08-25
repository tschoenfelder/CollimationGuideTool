from astrotool_core.target.detector import DetectionResult
from astrotool_core.target.point_source import PointSource
from astrotool_core.target.roi_selector import select_target


def _result(*sources: PointSource) -> DetectionResult:
    return DetectionResult(sources=sources, image_quality="usable", focus_warning=None, notes=())


def test_no_sources_returns_none() -> None:
    assert select_target(_result()) is None


def test_single_normal_star_is_selected() -> None:
    source = PointSource(x=10.0, y=10.0, peak=500.0, area=20, kind="normal_star")
    assert select_target(_result(source)) is source


def test_brightest_normal_star_is_preferred() -> None:
    dim = PointSource(x=10.0, y=10.0, peak=300.0, area=20, kind="normal_star")
    bright = PointSource(x=50.0, y=50.0, peak=900.0, area=22, kind="normal_star")
    assert select_target(_result(dim, bright)) is bright


def test_donut_like_sources_are_excluded() -> None:
    donut = PointSource(
        x=10.0, y=10.0, peak=2000.0, area=40, kind="defocus_candidate", donut_like=True
    )
    normal = PointSource(x=50.0, y=50.0, peak=500.0, area=20, kind="normal_star")
    assert select_target(_result(donut, normal)) is normal


def test_falls_back_to_brightest_saturated_star_when_no_normal_star_present() -> None:
    saturated = PointSource(
        x=10.0, y=10.0, peak=65000.0, area=30, kind="saturated_star", saturated=True
    )
    assert select_target(_result(saturated)) is saturated


def test_only_donut_like_sources_returns_none() -> None:
    donut = PointSource(
        x=10.0, y=10.0, peak=2000.0, area=40, kind="defocus_candidate", donut_like=True
    )
    assert select_target(_result(donut)) is None
