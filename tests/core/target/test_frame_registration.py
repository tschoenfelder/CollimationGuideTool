"""Tests for Stage 2 short-exposure frame registration — issue #16 AC
2.1-2.3: align successive focused-star frames to a common reference
position, tolerating brightness variation and moderate noise, rejecting
frames where the star can't be measured."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.frame_registration import register_frames, shift_image
from astrotool_core.testing.frame_factory import single_star_image, star_field_image

_SHAPE = (60, 80)


def _star(x: float, y: float, *, peak: float = 3000.0, background: float = 100.0) -> np.ndarray:
    return single_star_image(_SHAPE, x=x, y=y, peak=peak, sigma=2.0, background=background)


class TestShiftImage:
    def test_shifting_moves_a_star_to_the_expected_position(self) -> None:
        image = _star(30.0, 25.0)

        shifted = shift_image(image, dx=5.0, dy=-3.0)

        detection = detect_sources(shifted)
        assert len(detection.sources) == 1
        source = detection.sources[0]
        assert source.x == pytest.approx(35.0, abs=0.3)
        assert source.y == pytest.approx(22.0, abs=0.3)

    def test_zero_shift_is_a_near_identity(self) -> None:
        # Excludes the very last row/column: this bilinear technique
        # (matching terrestrial_registrar._bilinear_sample's own
        # established convention) treats a sample needing a pixel beyond
        # the frame's own far edge as "invalid" and fills it instead --
        # true even at zero shift, since interpolation always looks one
        # pixel ahead. Interior pixels reproduce exactly (zero shift
        # means integer-valued sample coordinates, zero interpolation
        # weight on the neighboring pixel).
        image = _star(30.0, 25.0)
        shifted = shift_image(image, dx=0.0, dy=0.0)
        assert shifted.shape == image.shape
        assert np.allclose(shifted[:-1, :-1], image[:-1, :-1], atol=1e-6)


class TestAC2_1TranslationalRegistration:
    def test_known_shifted_copies_align_to_a_common_position(self) -> None:
        base_x, base_y = 30.0, 25.0
        offsets = [(0.0, 0.0), (4.0, -3.0), (-5.0, 2.0), (3.0, 5.0), (-2.0, -4.0)]
        frames = [_star(base_x + dx, base_y + dy) for dx, dy in offsets]

        result = register_frames(frames)

        assert result.reference_position is not None
        assert len(result.usable_pixels) == len(frames)
        for pixels in result.usable_pixels:
            detection = detect_sources(pixels)
            assert len(detection.sources) == 1
            source = detection.sources[0]
            assert source.x == pytest.approx(result.reference_position[0], abs=0.5)
            assert source.y == pytest.approx(result.reference_position[1], abs=0.5)

    def test_reference_position_is_the_first_frames_own_measured_position(self) -> None:
        frames = [_star(30.0, 25.0), _star(34.0, 22.0)]

        result = register_frames(frames)

        assert result.reference_position is not None
        assert result.reference_position == pytest.approx((30.0, 25.0), abs=0.3)
        # The reference frame itself needs no real shift.
        assert result.outcomes[0].shift_dx == pytest.approx(0.0, abs=0.3)
        assert result.outcomes[0].shift_dy == pytest.approx(0.0, abs=0.3)


class TestAC2_2BrightnessVariation:
    def test_positions_recovered_despite_differing_intensity_scaling(self) -> None:
        base_x, base_y = 30.0, 25.0
        offsets_and_peaks = [
            (0.0, 0.0, 3000.0),
            (4.0, -3.0, 1500.0),
            (-5.0, 2.0, 6000.0),
            (3.0, 5.0, 900.0),
        ]
        frames = [
            _star(base_x + dx, base_y + dy, peak=peak) for dx, dy, peak in offsets_and_peaks
        ]

        result = register_frames(frames)

        assert result.reference_position is not None
        assert len(result.usable_pixels) == len(frames)
        for pixels in result.usable_pixels:
            detection = detect_sources(pixels)
            assert len(detection.sources) == 1
            source = detection.sources[0]
            assert source.x == pytest.approx(result.reference_position[0], abs=0.5)
            assert source.y == pytest.approx(result.reference_position[1], abs=0.5)


class TestAC2_3InvalidFrameRejection:
    def test_a_frame_without_a_detectable_star_is_marked_unusable(self) -> None:
        starless = star_field_image(_SHAPE, [], background=50.0)
        frames = [_star(30.0, 25.0), starless, _star(32.0, 27.0)]

        result = register_frames(frames)

        assert result.outcomes[1].usable is False
        assert result.outcomes[1].pixels is None
        assert result.outcomes[1].star is None
        assert len(result.usable_pixels) == 2

    def test_all_frames_starless_reports_no_reference_position(self) -> None:
        frames = [star_field_image(_SHAPE, [], background=50.0) for _ in range(3)]

        result = register_frames(frames)

        assert result.reference_position is None
        assert result.usable_pixels == ()
        assert all(not outcome.usable for outcome in result.outcomes)


class TestModerateNoiseTolerance:
    def test_positions_recovered_despite_moderate_noise(self) -> None:
        rng = np.random.default_rng(42)
        base_x, base_y = 30.0, 25.0
        offsets = [(0.0, 0.0), (4.0, -3.0), (-5.0, 2.0)]
        frames = [
            _star(base_x + dx, base_y + dy) + rng.normal(0.0, 15.0, size=_SHAPE)
            for dx, dy in offsets
        ]

        result = register_frames(frames)

        assert result.reference_position is not None
        assert len(result.usable_pixels) == len(frames)
        for pixels in result.usable_pixels:
            detection = detect_sources(pixels)
            assert len(detection.sources) >= 1
            source = detection.sources[0]
            assert source.x == pytest.approx(result.reference_position[0], abs=1.0)
            assert source.y == pytest.approx(result.reference_position[1], abs=1.0)
