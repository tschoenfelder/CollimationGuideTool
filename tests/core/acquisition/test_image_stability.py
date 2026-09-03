from __future__ import annotations

import numpy as np
from astrotool_core.acquisition.image_stability import (
    StabilityStatus,
    check_image_stability,
)


def _textured_image(shape: tuple[int, int], seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(loc=500.0, scale=80.0, size=shape)


class TestCheckImageStability:
    def test_identical_frames_are_stable(self) -> None:
        base = _textured_image((80, 80), seed=1)
        result = check_image_stability([base, base.copy(), base.copy()], tolerance_px=1.0)

        assert result.status is StabilityStatus.STABLE
        assert result.stable
        assert result.max_displacement_px == 0.0
        assert result.samples_checked == 3

    def test_a_small_shift_within_tolerance_is_stable(self) -> None:
        base = _textured_image((100, 100), seed=2)
        shifted = np.roll(base, shift=(1, 1), axis=(0, 1))
        result = check_image_stability([base, shifted], tolerance_px=2.0)

        assert result.status is StabilityStatus.STABLE
        assert result.max_displacement_px is not None
        assert result.max_displacement_px <= 2.0

    def test_a_large_shift_beyond_tolerance_is_unstable(self) -> None:
        base = _textured_image((100, 100), seed=3)
        shifted = np.roll(base, shift=(10, 8), axis=(0, 1))
        result = check_image_stability([base, shifted], tolerance_px=2.0)

        assert result.status is StabilityStatus.UNSTABLE
        assert not result.stable
        assert result.max_displacement_px is not None
        assert result.max_displacement_px > 2.0

    def test_the_worst_consecutive_pair_governs_not_the_net_displacement(self) -> None:
        """A sequence that drifts out and back to its starting position
        must still be flagged unstable -- net first-to-last displacement
        is zero, but the middle pair moved well beyond tolerance."""
        base = _textured_image((100, 100), seed=4)
        drifted = np.roll(base, shift=(10, 0), axis=(0, 1))
        back = base.copy()
        result = check_image_stability([base, drifted, back], tolerance_px=2.0)

        assert result.status is StabilityStatus.UNSTABLE

    def test_fewer_than_two_frames_is_insufficient_samples(self) -> None:
        base = _textured_image((50, 50), seed=5)
        assert check_image_stability([], tolerance_px=1.0).status == (
            StabilityStatus.INSUFFICIENT_SAMPLES
        )
        assert check_image_stability([base], tolerance_px=1.0).status == (
            StabilityStatus.INSUFFICIENT_SAMPLES
        )

    def test_min_samples_requires_more_than_two_frames(self) -> None:
        base = _textured_image((80, 80), seed=6)
        result = check_image_stability([base, base.copy()], tolerance_px=1.0, min_samples=3)
        assert result.status is StabilityStatus.INSUFFICIENT_SAMPLES

    def test_no_correlatable_content_is_indeterminate_not_stable(self) -> None:
        """Issue #30: never silently accept a frame merely because
        nothing could be measured -- a flat/saturated frame in the
        sequence must not read as "stable" by default."""
        flat = np.full((60, 60), 4095.0)
        result = check_image_stability([flat, flat.copy()], tolerance_px=1.0)

        assert result.status is StabilityStatus.INDETERMINATE
        assert not result.stable

    def test_an_indeterminate_pair_short_circuits_the_whole_result(self) -> None:
        base = _textured_image((80, 80), seed=7)
        flat = np.full((80, 80), 4095.0)
        result = check_image_stability([base, base.copy(), flat], tolerance_px=1.0)

        assert result.status is StabilityStatus.INDETERMINATE
