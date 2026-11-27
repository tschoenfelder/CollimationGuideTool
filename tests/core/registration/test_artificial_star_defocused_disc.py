"""Field bundle 10e89885 (2026-11-26): "Calibrate FOV" showed no progress for minutes.

The artificial star filled a third of the Main frame as a defocused, speckled, partly
clipped disc. The generic star detector split it into 7953 "stars" (5 minutes of search on
a PC) and the mode needs exactly one. In Guide the one star was a ringed disc, far from the
nominally predicted position because the two cameras are not co-aligned.
"""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.registration.artificial_star_registrar import ArtificialStarRegistrar
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import RegistrationStatus

_MAIN = OpticalPrior(name="a", sensor_width_px=960, sensor_height_px=540, pixel_scale_arcsec=0.3)
_GUIDE = OpticalPrior(name="b", sensor_width_px=960, sensor_height_px=540, pixel_scale_arcsec=3.0)


def _speckled_donut(
    shape: tuple[int, int], center: tuple[float, float], outer: float, inner: float, seed: int
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    radius = np.hypot(xx - center[0], yy - center[1])
    ring = np.where(radius <= outer, 40000.0, 0.0) * np.where(radius <= inner, 0.4, 1.0)
    speckle = rng.uniform(0.5, 1.8, size=shape)  # the brightest speckles clip
    noisy = 3000.0 + ring * speckle + rng.normal(0.0, 300.0, size=shape)
    return np.clip(noisy, 0.0, 65535.0).astype(np.float32)


def _ringed_disc(
    shape: tuple[int, int], center: tuple[float, float], radius_px: float
) -> np.ndarray:
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    radius = np.hypot(xx - center[0], yy - center[1])
    rings = 0.65 + 0.35 * np.cos(radius * 1.1)
    spider = np.where(np.abs(yy - center[1]) < 3, 0.25, 1.0)
    brighter_left = 1.0 - 0.4 * (xx - center[0]) / radius_px
    disc = np.where(radius <= radius_px, 2000.0 * rings * spider * brighter_left, 0.0)
    return (20.0 + disc).astype(np.float32)


def test_a_large_defocused_disc_is_one_star_in_each_camera() -> None:
    main = _speckled_donut((540, 960), center=(620.0, 230.0), outer=200.0, inner=80.0, seed=1)
    guide = _ringed_disc((540, 960), center=(200.0, 170.0), radius_px=42.0)

    result = ArtificialStarRegistrar().register(main, guide, _MAIN, _GUIDE)

    assert result.ok, (result.status, result.diagnostics.get("candidate_count_a"))
    assert result.diagnostics["reference_point"] == pytest.approx((620.0, 230.0), abs=6.0)
    assert result.diagnostics["matched_point"] == pytest.approx((200.0, 170.0), abs=4.0)


def test_two_discs_in_main_are_still_not_one_artificial_star() -> None:
    main = np.maximum(
        _speckled_donut((540, 960), center=(250.0, 270.0), outer=110.0, inner=40.0, seed=2),
        _speckled_donut((540, 960), center=(700.0, 270.0), outer=110.0, inner=40.0, seed=3),
    )
    guide = _ringed_disc((540, 960), center=(480.0, 270.0), radius_px=42.0)

    result = ArtificialStarRegistrar().register(main, guide, _MAIN, _GUIDE)

    assert not result.ok
    assert result.status is RegistrationStatus.INSUFFICIENT_STARS
    assert result.diagnostics["candidate_count_a"] == 2
