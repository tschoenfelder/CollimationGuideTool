"""Issue #43: the stability check keeps the estimator's evidence (weakest score and
tier per pair) so a pulled bundle can tell a genuinely still image from a confident
zero-lag correlation of fixed structure -- the verdict itself is unchanged."""

from __future__ import annotations

import numpy as np
from astrotool_core.acquisition.image_stability import StabilityStatus, check_image_stability


def _scene(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random((96, 128)).astype(np.float64)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0)


class TestEvidence:
    def test_a_still_image_reports_a_confident_score_and_the_tier_per_pair(self) -> None:
        scene = _scene()

        result = check_image_stability([scene, scene, scene], tolerance_px=3.0)

        assert result.status is StabilityStatus.STABLE
        assert result.max_displacement_px == 0.0
        assert result.min_score is not None and result.min_score > 0.95
        assert result.tiers == ("full_res", "full_res")  # one entry per consecutive pair

    def test_the_weakest_pair_defines_min_score(self) -> None:
        rng = np.random.default_rng(3)
        scene = _scene()
        noisy = scene + rng.normal(0.0, 60.0, scene.shape)  # a degraded frame

        result = check_image_stability([scene, scene, noisy], tolerance_px=3.0)

        assert result.min_score is not None
        assert result.min_score < 0.999  # not the perfect identical-frame score

    def test_a_moving_image_is_still_unstable_and_keeps_its_evidence(self) -> None:
        scene = _scene()

        result = check_image_stability(
            [scene, np.roll(scene, 20, axis=1), np.roll(scene, 40, axis=1)], tolerance_px=3.0
        )

        assert result.status is StabilityStatus.UNSTABLE
        assert result.max_displacement_px == 20.0
        assert result.min_score is not None and len(result.tiers) == 2

    def test_no_correlatable_content_is_indeterminate_with_its_tiers(self) -> None:
        flat = np.full((96, 128), 500.0)

        result = check_image_stability([flat, flat, flat], tolerance_px=3.0)

        assert result.status is StabilityStatus.INDETERMINATE
        assert result.tiers  # the tier that failed is recorded

    def test_too_few_samples_has_no_evidence_to_report(self) -> None:
        result = check_image_stability([_scene()], tolerance_px=3.0)

        assert result.status is StabilityStatus.INSUFFICIENT_SAMPLES
        assert result.min_score is None and result.tiers == ()
