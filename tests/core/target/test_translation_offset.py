import numpy as np
import pytest
from astrotool_core.target.translation_offset import measure_translation_offset


def _textured_image(shape: tuple[int, int], seed: int) -> np.ndarray:
    """Not a star field -- plain textured noise, standing in for the kind
    of ordinary terrestrial content this module is for (see its own
    docstring's incident 6fa2aa59 reference)."""
    rng = np.random.default_rng(seed)
    return rng.normal(loc=500.0, scale=80.0, size=shape)


def test_a_known_shift_is_recovered_exactly() -> None:
    before = _textured_image((128, 128), seed=1)
    after = np.roll(before, shift=(3, -5), axis=(0, 1))  # (dy, dx)

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert offset.dx_px == -5.0
    assert offset.dy_px == 3.0
    assert offset.score > 0.9


def test_zero_shift_reports_near_zero_offset_and_a_high_score() -> None:
    image = _textured_image((128, 128), seed=2)

    offset = measure_translation_offset(image, image.copy())

    assert offset is not None
    assert offset.dx_px == 0.0
    assert offset.dy_px == 0.0
    assert offset.score > 0.9


def test_a_negative_wraparound_shift_is_recovered() -> None:
    before = _textured_image((128, 128), seed=3)
    after = np.roll(before, shift=(-40, 60), axis=(0, 1))

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert offset.dx_px == 60.0
    assert offset.dy_px == -40.0


def test_a_constant_brightness_offset_does_not_move_the_detected_peak() -> None:
    """Mean-subtraction's whole point (see module docstring: incident
    a4ffe048's fix) -- an exposure/gain difference between two real
    captures must not bias the match toward wherever the frame happens
    to be brightest."""
    before = _textured_image((128, 128), seed=1)
    after = np.roll(before, shift=(3, -5), axis=(0, 1)) + 500.0

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert offset.dx_px == -5.0
    assert offset.dy_px == 3.0


def test_two_unrelated_images_report_no_usable_match() -> None:
    """The actual ceiling `_DEFAULT_MIN_SCORE` has to clear, not the
    flat-array test below: two independent-per-pixel-noise images (not
    flat) have their own nonzero noise floor -- measured up to ~0.0385
    across several seed pairs when this module's normalized-cross-
    correlation implementation replaced the original whitened phase
    correlation (incident a4ffe048). `_weak_signal_pair` below's ~0.3
    real-looking match sits with a wide margin above that floor."""
    before = _textured_image((128, 128), seed=4)
    after = _textured_image((128, 128), seed=5)

    assert measure_translation_offset(before, after) is None


def test_two_flat_featureless_images_report_no_usable_match() -> None:
    """Exactly incident 6fa2aa59's real case: a saturated/clipped ("no
    real content") capture must not report a spurious displacement --
    both frames have zero variance, so there's nothing to normalize by."""
    before = np.full((64, 64), 4095.0)
    after = np.full((64, 64), 4095.0)

    assert measure_translation_offset(before, after) is None


def _weak_signal_pair(
    shape: tuple[int, int], *, shift: tuple[int, int] = (0, 0), signal_fraction: float = 0.25
) -> tuple[np.ndarray, np.ndarray]:
    """A genuinely-matching pair whose correlation score sits in a
    moderate range, well clear of both `_DEFAULT_MIN_SCORE` and the
    near-1.0 scores `_textured_image`'s own exact-shift tests produce --
    unlike those (independent-per-pixel Gaussian, no real degradation),
    this blends a shifted copy of a shared signal into mostly
    independent per-frame noise, so there's a realistic "confident but
    not perfect" case among the fixtures too. `before` is the pure
    shared signal; `after` blends a shifted copy of that same signal
    into mostly independent noise -- using the *same* noise array for
    both frames would collapse a shift=(0, 0) pair to bit-identical
    arrays and trivially score 1.0, which isn't what this fixture is for."""
    shared = np.random.default_rng(101).normal(loc=500.0, scale=80.0, size=shape)
    noise = np.random.default_rng(102).normal(loc=0.0, scale=80.0, size=shape)
    before = shared
    shifted_shared = np.roll(shared, shift=shift, axis=(0, 1))
    after = shifted_shared * signal_fraction + noise * (1 - signal_fraction)
    return before, after


def test_a_moderate_confidence_match_is_accepted() -> None:
    before, after = _weak_signal_pair((128, 128))

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert offset.dx_px == 0.0
    assert offset.dy_px == 0.0
    assert 0.15 < offset.score < 0.9  # clear of both the floor and a "perfect" score


def test_a_moderate_confidence_shifted_match_is_recovered() -> None:
    before, after = _weak_signal_pair((128, 128), shift=(4, -7))

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert offset.dx_px == -7.0
    assert offset.dy_px == 4.0
    assert offset.score > 0.15


def test_mismatched_shapes_raise() -> None:
    before = _textured_image((64, 64), seed=6)
    after = _textured_image((32, 32), seed=7)
    with pytest.raises(ValueError, match="same shape"):
        measure_translation_offset(before, after)


def test_non_2d_arrays_raise() -> None:
    before = np.zeros(64)
    after = np.zeros(64)
    with pytest.raises(ValueError, match="2D"):
        measure_translation_offset(before, after)
