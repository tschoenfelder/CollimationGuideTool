"""Issue #39: an explicit collimation target mode (natural vs artificial
star) so the UI never leaves the user to infer what a measurement used."""

from __future__ import annotations

import numpy as np
from astrotool_core.frames.analysis_plane import build_analysis_plane
from astrotool_core.testing.frame_factory import donut_image, make_frame
from collimation_tool.domain.collimation_measurement import DonutAnalyzer
from collimation_tool.domain.target_mode import CollimationTargetMode


class TestCollimationTargetMode:
    def test_labels_name_the_source_explicitly(self) -> None:
        assert CollimationTargetMode.NATURAL_STAR.label == "Natural star"
        assert CollimationTargetMode.ARTIFICIAL_STAR.label == "Artificial star"

    def test_only_the_artificial_star_needs_the_finite_distance_caveat(self) -> None:
        assert not CollimationTargetMode.NATURAL_STAR.finite_distance
        assert CollimationTargetMode.ARTIFICIAL_STAR.finite_distance


class TestRoughCollimationIsSourceAgnostic:
    def test_a_defocused_artificial_star_donut_measures_without_any_sky_context(self) -> None:
        # Pure image-space input: no tracking state, no coordinates, no ASTAP.
        image = donut_image(
            (240, 240), outer_center=(120.0, 120.0), outer_radius=50.0,
            inner_center=(126.0, 118.0), inner_radius=20.0, peak=3000.0,
        )
        result = DonutAnalyzer().analyze(build_analysis_plane(make_frame(image)))

        assert result.reason == "ok"
        assert result.measurement is not None
        assert isinstance(image, np.ndarray)
