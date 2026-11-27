"""Field bundles 10e89885 / 64621320 (2026-11-26): a large defocused artificial star stalled
fine collimation (and the same generic star search served artificial-star calibration)."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.diffraction.optical_reference_model import OpticalConfig
from astrotool_core.focus.disc_locator import locate_main_disc
from collimation_tool.application.fine_collimation_controller import FineCollimationController
from collimation_tool.application.mount_align_measurement import measure
from collimation_tool.application.star_acquisition import FocusedStarAcquisition


def _speckled_donut(x: float, y: float, outer: float) -> np.ndarray:
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:1080, 0:1920]
    radius = np.hypot(xx - x, yy - y)
    ring = np.where(radius <= outer, 40000.0, 0.0) * np.where(radius <= 0.4 * outer, 0.4, 1.0)
    frame = 3000.0 + ring * rng.uniform(0.5, 1.8, ring.shape) + rng.normal(0, 300.0, ring.shape)
    return np.clip(frame, 0.0, 65535.0).astype(np.float32)


def test_the_main_disc_is_located_on_a_large_frame() -> None:
    disc = locate_main_disc(_speckled_donut(1250.0, 460.0, 400.0))

    assert disc is not None
    assert (disc.x, disc.y) == pytest.approx((1250.0, 460.0), abs=10.0)
    assert disc.radius_px == pytest.approx(400.0, rel=0.05)


def test_artificial_star_calibration_measures_the_disc_centre() -> None:
    position = measure("terrestrial", _speckled_donut(700.0, 500.0, 300.0), artificial_star=True)

    assert position == pytest.approx((700.0, 500.0), abs=10.0)


def test_fine_collimation_refuses_a_defocused_disc_at_once_and_says_what_to_do() -> None:
    frame = _speckled_donut(960.0, 540.0, 400.0)
    controller = FineCollimationController(
        FocusedStarAcquisition(roi_size=(64, 64)),
        get_frame=lambda: frame,
        optical_config=OpticalConfig(),
    )

    outcome = controller.run()

    assert outcome.status == "failed"
    assert outcome.reason == "star_too_defocused"
    assert outcome.detail is not None
    assert "rough collimation" in outcome.detail
