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


def test_two_unrelated_images_report_no_usable_match() -> None:
    """This is the actual ceiling `_DEFAULT_MIN_SCORE` has to clear, not
    the flat-array test below: two independent-per-pixel-noise images
    (not flat) have their own nonzero noise floor -- measured ~0.03-0.036
    across several seed pairs when `_DEFAULT_MIN_SCORE` was recalibrated
    for incident a082144a. `_weak_signal_pair` above's ~0.06-0.07 real
    matches sit safely above that floor with margin on both sides."""
    before = _textured_image((128, 128), seed=4)
    after = _textured_image((128, 128), seed=5)

    assert measure_translation_offset(before, after) is None


def test_two_flat_featureless_images_report_no_usable_match() -> None:
    """Exactly incident 6fa2aa59's real case: a saturated/clipped ("no
    real content") capture must not report a spurious displacement."""
    before = np.full((64, 64), 4095.0)
    after = np.full((64, 64), 4095.0)

    assert measure_translation_offset(before, after) is None


def _weak_signal_pair(
    shape: tuple[int, int], *, shift: tuple[int, int] = (0, 0), signal_fraction: float = 0.08
) -> tuple[np.ndarray, np.ndarray]:
    """A genuinely-matching pair whose correlation score sits in the
    moderate range real camera frames actually score in (see
    `_DEFAULT_MIN_SCORE`'s own docstring: incident a082144a measured a
    real, correctly-recovered match at ~0.07) -- unlike
    `_textured_image`'s independent-per-pixel Gaussian noise, where even
    an *unrelated* pair sits around 0.03-0.036 and a real match scores
    >0.9, leaving no representative "moderate but genuine" case among
    the existing fixtures. `before` is the pure shared signal; `after`
    blends a shifted copy of that same signal into mostly independent
    noise -- `signal_fraction=0.08` keeps the two frames' own noise
    genuinely uncorrelated (unlike blending both frames from the same
    noise array, which collapses a shift=(0, 0) pair to bit-identical
    arrays and trivially scores 1.0 -- not what this fixture is for)."""
    shared = np.random.default_rng(101).normal(loc=500.0, scale=80.0, size=shape)
    noise = np.random.default_rng(102).normal(loc=0.0, scale=80.0, size=shape)
    before = shared
    shifted_shared = np.roll(shared, shift=shift, axis=(0, 1))
    after = shifted_shared * signal_fraction + noise * (1 - signal_fraction)
    return before, after


def test_a_moderate_confidence_real_looking_match_is_accepted() -> None:
    """Regression test for incident a082144a: a real camera's genuine,
    correctly-recovered match doesn't score anywhere near the >0.9 the
    existing synthetic-noise fixtures produce -- "not enough structure"
    must not fire just because a real match's confidence is moderate
    rather than near-perfect."""
    before, after = _weak_signal_pair((128, 128))

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert offset.dx_px == 0.0
    assert offset.dy_px == 0.0
    assert offset.score > 0.05  # clear of the synthetic-noise floor with margin


def test_a_moderate_confidence_shifted_match_is_recovered() -> None:
    before, after = _weak_signal_pair((128, 128), shift=(4, -7))

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert offset.dx_px == -7.0
    assert offset.dy_px == 4.0
    assert offset.score > 0.05


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
