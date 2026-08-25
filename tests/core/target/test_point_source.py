from astrotool_core.target.point_source import PointSource


def test_defaults() -> None:
    source = PointSource(x=1.0, y=2.0, peak=500.0, area=12, kind="normal_star")
    assert source.fwhm_x is None
    assert source.fwhm_y is None
    assert source.saturated is False
    assert source.donut_like is False


def test_is_immutable() -> None:
    import dataclasses

    import pytest

    source = PointSource(x=1.0, y=2.0, peak=500.0, area=12, kind="normal_star")
    with pytest.raises(dataclasses.FrozenInstanceError):
        source.x = 5.0  # type: ignore[misc]
