"""Issue #37 additional AC (S7): hot-pixel / noise rejection is tested separately from
legitimate single-star acceptance.

`test_artificial_star_registrar.py` covers saturated, donut-like and edge-clipped
sources, but not a single hot pixel or plain sensor noise. A hot pixel must never be
taken for the artificial star, and it must not turn a valid one-star frame into a
"several candidates" failure."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.registration.artificial_star_registrar import ArtificialStarRegistrar
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import RegistrationStatus
from astrotool_core.testing.frame_factory import single_star_image

_MAIN = OpticalPrior(name="a", sensor_width_px=120, sensor_height_px=120, pixel_scale_arcsec=1.0)
_GUIDE = OpticalPrior(name="b", sensor_width_px=400, sensor_height_px=400, pixel_scale_arcsec=1.0)
_HOT_VALUE = 4000.0  # well above the background, below the 16-bit clip


def _noisy(shape: tuple[int, int], seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (100.0 + rng.normal(0.0, 3.0, size=shape)).astype(np.float32)


def test_pure_noise_has_no_artificial_star() -> None:
    result = ArtificialStarRegistrar().register(
        _noisy((120, 120), 1), _noisy((400, 400), 2), _MAIN, _GUIDE
    )

    assert not result.ok
    assert result.status is RegistrationStatus.INSUFFICIENT_STARS


def test_a_lone_hot_pixel_is_never_accepted_as_the_artificial_star() -> None:
    frame_a = _noisy((120, 120), 3)
    frame_a[70, 40] = _HOT_VALUE
    frame_b = _noisy((400, 400), 4)
    frame_b[210, 180] = _HOT_VALUE

    result = ArtificialStarRegistrar().register(frame_a, frame_b, _MAIN, _GUIDE)

    assert not result.ok
    assert result.status is RegistrationStatus.INSUFFICIENT_STARS


def test_a_hot_pixel_beside_the_real_star_does_not_make_it_ambiguous() -> None:
    frame_a = single_star_image((120, 120), x=60.0, y=60.0, peak=5000.0, sigma=2.0)
    frame_a[30, 90] = _HOT_VALUE
    frame_b = single_star_image((400, 400), x=220.0, y=190.0, peak=5000.0, sigma=2.0)
    frame_b[200, 240] = _HOT_VALUE  # inside the match tolerance around the prediction

    result = ArtificialStarRegistrar().register(frame_a, frame_b, _MAIN, _GUIDE)

    assert result.ok, (result.status, result.diagnostics)
    assert result.diagnostics["matched_point"] == pytest.approx((220.0, 190.0), abs=1.0)
