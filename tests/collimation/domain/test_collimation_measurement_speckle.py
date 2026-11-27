"""Field bundle 64621320 (2026-11-26): on the speckled artificial-star donut both fitted
rings lay well inside the real edges (outer radius 700 instead of about 920, shadow 517
instead of about 290), so the reported mismatch could not be trusted."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.frames.analysis_plane import AnalysisPlane
from collimation_tool.domain.collimation_measurement import DonutAnalyzer


def _speckled_donut(hole_dx: float, hole_dy: float) -> np.ndarray:
    rng = np.random.default_rng(7)
    yy, xx = np.mgrid[0:1080, 0:1920]
    outer = np.hypot(xx - 1000.0, yy - 540.0) <= 450.0
    hole = np.hypot(xx - 1000.0 - hole_dx, yy - 540.0 - hole_dy) <= 150.0
    illumination = 1.0 + 0.25 * (xx - 1000.0) / 450.0  # brighter to the right
    ring = 30000.0 * outer * np.where(hole, 0.15, 1.0) * illumination
    speckle = np.repeat(np.repeat(rng.uniform(0.4, 1.6, (270, 480)), 4, axis=0), 4, axis=1)
    frame = 2000.0 + ring * speckle + rng.normal(0.0, 300.0, ring.shape)
    pixels: np.ndarray = np.clip(frame, 0.0, 65535.0).astype(np.float32)
    return pixels


@pytest.mark.parametrize(("hole_dx", "hole_dy"), [(-30.0, 0.0), (0.0, 25.0), (18.0, -22.0)])
def test_both_edges_and_the_mismatch_are_found_on_a_speckled_donut(
    hole_dx: float, hole_dy: float
) -> None:
    pixels = _speckled_donut(hole_dx, hole_dy)

    result = DonutAnalyzer().analyze(
        AnalysisPlane(
            raw=pixels, mono=pixels, bit_depth=16, width=1920, height=1080, timestamp=0.0
        )
    )

    measurement = result.measurement
    assert measurement is not None, result.reason
    assert measurement.outer_ring.radius_x == pytest.approx(450.0, rel=0.05)
    assert measurement.inner_hole.radius_x == pytest.approx(150.0, rel=0.1)
    assert (measurement.outer_ring.center_x, measurement.outer_ring.center_y) == pytest.approx(
        (1000.0, 540.0), abs=8.0
    )
    assert (measurement.error_x_px, measurement.error_y_px) == pytest.approx(
        (hole_dx, hole_dy), abs=8.0
    )
