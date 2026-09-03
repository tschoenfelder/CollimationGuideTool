from __future__ import annotations

import pytest
from astrotool_core.registration.optical_prior import OpticalPrior, scale_ratio


class TestOpticalPrior:
    def test_computes_fov_in_arcsec(self) -> None:
        prior = OpticalPrior(name="main", sensor_width_px=100, sensor_height_px=50,
                              pixel_scale_arcsec=2.0)
        assert prior.fov_width_arcsec == 200.0
        assert prior.fov_height_arcsec == 100.0

    def test_non_positive_sensor_dimensions_raise(self) -> None:
        with pytest.raises(ValueError, match="sensor dimensions"):
            OpticalPrior(name="main", sensor_width_px=0, sensor_height_px=50,
                         pixel_scale_arcsec=2.0)

    def test_non_positive_pixel_scale_raises(self) -> None:
        with pytest.raises(ValueError, match="pixel_scale_arcsec"):
            OpticalPrior(name="main", sensor_width_px=100, sensor_height_px=50,
                         pixel_scale_arcsec=0.0)


class TestScaleRatio:
    def test_finer_source_scale_yields_a_ratio_below_one(self) -> None:
        # Real rig shape: main's plate scale (0.38"/px) is finer than
        # guide's (3.32"/px) -- one main pixel covers far less sky, so
        # it's a small fraction of one guide pixel.
        main = OpticalPrior(name="main", sensor_width_px=100, sensor_height_px=100,
                             pixel_scale_arcsec=0.38)
        guide = OpticalPrior(name="guide", sensor_width_px=100, sensor_height_px=100,
                              pixel_scale_arcsec=3.32)
        ratio = scale_ratio(main, guide)
        assert ratio == pytest.approx(0.38 / 3.32)
        assert ratio < 1.0

    def test_identical_scale_yields_a_ratio_of_one(self) -> None:
        a = OpticalPrior(name="a", sensor_width_px=50, sensor_height_px=50,
                          pixel_scale_arcsec=1.5)
        b = OpticalPrior(name="b", sensor_width_px=80, sensor_height_px=80,
                          pixel_scale_arcsec=1.5)
        assert scale_ratio(a, b) == pytest.approx(1.0)
