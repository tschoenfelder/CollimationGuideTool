import numpy as np
import pytest
from astrotool_core.target.translation_offset import (
    max_unaliased_shift_px,
    measure_translation_offset,
    measure_translation_offset_with_tier,
)
from astrotool_core.testing import CI_SHIFT_GRID_SUBSET, ShiftCase, StarSpec, star_field_image


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


def _pair_with_saturated_scene_region(
    *, saturated_rows: int, seed: int, shape: tuple[int, int] = (128, 128)
) -> tuple[np.ndarray, np.ndarray]:
    """A known-shift pair where part of the *scene itself* (not a static
    screen-fixed overlay) is saturated -- the saturated region pans along
    with everything else via the same `np.roll`, matching how a real
    blown-out sky region moves with the rest of a real panned frame.
    Deliberately not a region fixed at the same absolute location in both
    `before`/`after`: that would inject an artificial, always-perfectly-
    self-matching zero-shift anchor that has nothing to do with real
    saturation (confirmed empirically while building this test -- it
    broke even a single-digit-pixel patch, an artifact of the test
    construction, not of real saturation)."""
    scene = _textured_image(shape, seed=seed)
    scene[:saturated_rows, :] = 4095.0
    before = scene
    after = np.roll(scene, shift=(3, -5), axis=(0, 1))
    return before, after


def test_a_heavily_saturated_frame_pair_reports_no_usable_match() -> None:
    """Real incident ef49ecb1-a052-44ea-b45e-01145a9f0c33: a real
    ~12-26%-saturated guide-camera pair scored confidently (0.98, 0.71,
    both above _DEFAULT_MIN_SCORE) while reporting a wrong dx_px=0.0 for
    two independent, roughly-orthogonal real mount-axis pulses -- see
    _DEFAULT_MAX_SATURATED_FRACTION's own docstring. This synthetic pair
    (~31% saturated, well above the 5% threshold) pins the new guard's
    own mechanical behavior directly -- reproducing the real incident's
    own subtle correlation-ambiguity mechanism synthetically turned out
    not to be feasible (an exact np.roll shift, even with independent
    per-frame noise layered on top, always still recovers the correct
    shift perfectly regardless of how much of the *scene* is saturated,
    unlike a real camera's non-circular, imperfect real-world capture --
    see tests/local_data/test_translation_offset_against_real_captures.py
    for the actual real-frame regression proving the real incident itself
    is fixed)."""
    before, after = _pair_with_saturated_scene_region(saturated_rows=40, seed=8)  # ~31%

    assert measure_translation_offset(before, after) is None


def test_a_lightly_saturated_frame_pair_still_reports_a_match() -> None:
    """Below _DEFAULT_MAX_SATURATED_FRACTION -- must not regress an
    ordinary frame with a small bright region (e.g. a lamp, a small sky
    patch) that legitimately clips a few pixels."""
    before, after = _pair_with_saturated_scene_region(saturated_rows=3, seed=9)  # ~2.3%

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert offset.dx_px == -5.0
    assert offset.dy_px == 3.0


def _smooth_scene(shape: tuple[int, int], seed: int, block: int = 16) -> np.ndarray:
    """Genuine broadband-but-correlated low-frequency structure -- unlike
    `_textured_image`'s plain per-pixel noise (every pixel statistically
    independent of its neighbors, nothing for a low-pass filter to
    preferentially preserve), this is a coarse random field expanded back
    up to `shape` in flat blocks, standing in for a real scene's actual
    spatially-coherent features (e.g. the diagonal cable/branch-like
    streaks real incident 93ba361f's own frames show)."""
    rng = np.random.default_rng(seed)
    small_shape = (shape[0] // block + 1, shape[1] // block + 1)
    small = rng.normal(loc=500.0, scale=150.0, size=small_shape)
    return np.kron(small, np.ones((block, block)))[: shape[0], : shape[1]]


def _noisy_shifted_scene_pair(
    shape: tuple[int, int], *, shift: tuple[int, int], seed: int, noise_scale: float
) -> tuple[np.ndarray, np.ndarray]:
    """A known-shift pair built from `_smooth_scene` with heavy,
    independent-per-frame sensor-noise-like Gaussian noise layered on top
    of *each* frame separately (not shared between them, unlike the
    shift itself) -- real incident 93ba361f-18c6-46f6-9a53-fd05be821b01's
    own real Main-camera frames were this noisy relative to their own
    real (broadband, lower-frequency) structure. `shift` should be a
    multiple of `_FALLBACK_DOWNSAMPLE_FACTOR` (8) so the downsampled
    fallback can recover it exactly rather than only approximately."""
    scene = _smooth_scene(shape, seed=seed)
    before = scene + np.random.default_rng(seed * 10 + 1).normal(0.0, noise_scale, size=shape)
    after = np.roll(scene, shift=shift, axis=(0, 1)) + np.random.default_rng(
        seed * 10 + 2
    ).normal(0.0, noise_scale, size=shape)
    return before, after


def test_a_noisy_large_frame_pair_recovers_the_shift_via_the_downsampled_fallback() -> None:
    """Real incident 93ba361f-18c6-46f6-9a53-fd05be821b01: a real
    terrestrial Main-camera frame pair with heavy per-pixel sensor noise
    (visually confirmed real structure present -- diagonal cable/branch-
    like streaks clearly visible to a human eye) scored only 0.098/0.093
    at full resolution for its two real axis pulses -- both well below
    _DEFAULT_MIN_SCORE, because independent per-pixel noise dominates
    this module's own whole-frame energy normalization. See
    _FALLBACK_DOWNSAMPLE_FACTOR's own docstring for why a plain
    per-pixel-noise `_textured_image` fixture (used by every *other* test
    in this file) can't reproduce this: it has no genuine low-frequency
    structure for a low-pass filter to preferentially preserve over the
    *added* noise, since both are the same statistical process. This
    fixture is deliberately large (480x640, above
    _FALLBACK_MIN_FRAME_SIDE_PX) -- the fallback is disabled below that
    threshold on purpose (see that constant's own docstring).

    Real request: reduce noise so a real shift correlates *better*, not
    just *at all* -- once the coarse x8 match is validated, the full-
    resolution peak's own (still slightly noisy) location is preferred
    over the coarse answer's `_FALLBACK_DOWNSAMPLE_FACTOR`-pixel
    granularity whenever the two agree (see
    `_FULL_RES_AGREEMENT_TOLERANCE_PX`'s own docstring) -- close to the
    true shift, not necessarily bit-exact the way the coarse, blocky
    x8 position happened to be for this fixture's exact-multiple-of-8
    shift."""
    before, after = _noisy_shifted_scene_pair(
        (480, 640), shift=(16, -24), seed=42, noise_scale=700.0
    )

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert abs(offset.dx_px - (-24.0)) <= 2.0
    assert abs(offset.dy_px - 16.0) <= 2.0
    assert offset.score >= 0.15  # _DEFAULT_MIN_SCORE


def _shared_low_frequency_field(
    shape: tuple[int, int], *, seed: int, block: int, scale: float
) -> np.ndarray:
    """A single coarse random field expanded back up to `shape` in flat
    blocks -- unlike `_smooth_scene`, this is meant to be added
    *identically* (unshifted) into both `before` and `after` below,
    standing in for a real broad, low-frequency-similar gradient (e.g.
    vignetting, or the genuinely featureless/hazy real scene real
    incident 6cb859d2-7a94-4e44-8aff-585f0bf2466b's own frames showed)
    that has nothing to do with any real shift."""
    rng = np.random.default_rng(seed)
    small_shape = (shape[0] // block + 1, shape[1] // block + 1)
    small = rng.normal(loc=0.0, scale=scale, size=small_shape)
    return np.kron(small, np.ones((block, block)))[: shape[0], : shape[1]]


def test_a_still_climbing_downsampled_match_is_rejected_despite_clearing_min_score() -> None:
    """Real incident 6cb859d2-7a94-4e44-8aff-585f0bf2466b, the very next
    real pair to hit the x8 fallback after it shipped: a genuinely
    featureless/hazy real Main-camera pair scored only 0.036 at full
    resolution (barely above the pure-unrelated-noise floor for this
    sensor size), yet the x8 fallback alone produced a *confident* but
    spurious (dx=0, dy=0) match (score 0.62) -- a large, broad,
    low-frequency-similar component (unrelated to any real shift) that
    only reveals itself as spurious by continuing to climb steeply
    toward 1.0 at coarser resolutions still, rather than plateauing the
    way a genuine match does. This fixture reproduces that shape: an
    unshifted shared low-frequency field (no real shift at all) added to
    unrelated per-frame higher-frequency content -- full-resolution score
    clears the noise floor but stays below _DEFAULT_MIN_SCORE, the x8
    fallback alone would confidently (score > 0.15) but wrongly report
    (0, 0), and _FALLBACK_VALIDATION_FACTOR's own plateau check must
    catch it (see that constant's own docstring for the real growth-rate
    evidence -- ~1.08-1.09 for a genuine match vs. ~1.38-1.40 here)."""
    shape = (480, 640)
    shared = _shared_low_frequency_field(shape, seed=1, block=128, scale=80.0)
    before = shared + _smooth_scene(shape, seed=10, block=4)
    after = shared + _smooth_scene(shape, seed=20, block=4)

    assert measure_translation_offset(before, after) is None


def test_the_fallback_is_disabled_below_the_minimum_frame_size() -> None:
    """This file's own small (128x128) fixtures must never exercise the
    downsampled fallback -- its false-positive floor for genuinely
    unrelated content was only verified safe down to real-camera-scale
    frames (see _FALLBACK_MIN_FRAME_SIDE_PX's own docstring), not a
    fixture this small. A pair too noisy to match at full resolution must
    still report no usable match, not silently fall back to a
    (potentially spurious) downsampled attempt."""
    before, after = _noisy_shifted_scene_pair(
        (128, 128), shift=(8, -8), seed=42, noise_scale=700.0
    )

    assert measure_translation_offset(before, after) is None


def test_two_unrelated_large_scenes_still_report_no_usable_match_after_the_fallback() -> None:
    """The downsampled fallback must not manufacture a false-positive
    match for genuinely unrelated content just because it's large enough
    to attempt -- two independent _smooth_scene draws, no shared shift at
    all, at the same size the recovery test above uses."""
    before = _smooth_scene((480, 640), seed=100)
    after = _smooth_scene((480, 640), seed=200)

    assert measure_translation_offset(before, after) is None


class TestMeasureTranslationOffsetWithTier:
    """Issues #28/#32: `scripts/translation_estimator_benchmark.py` and any
    future internal strategy (e.g. a sparse-content fallback) need to know
    *which* of this module's internal tiers actually produced a result --
    without that, a benchmark report can't tell "full-resolution correlation
    nailed it" apart from "only the noisy-fallback path saved it", and a
    future tier can't be evaluated in isolation. `measure_translation_offset_
    with_tier()` is a thin, additive wrapper around the exact same shared
    implementation `measure_translation_offset()` itself calls -- diagnostic
    metadata about the ONE estimator's own internal strategy selection, not a
    second production estimator (see this module's own docstring)."""

    def test_reports_full_res_for_a_confident_full_resolution_match(self) -> None:
        before = _textured_image((128, 128), seed=1)
        after = np.roll(before, shift=(3, -5), axis=(0, 1))

        offset, tier = measure_translation_offset_with_tier(before, after)

        assert offset is not None
        assert tier == "full_res"

    def test_reports_fallback_x8_when_only_the_downsampled_retry_clears_min_score(self) -> None:
        before, after = _noisy_shifted_scene_pair(
            (480, 640), shift=(16, -24), seed=42, noise_scale=700.0
        )

        offset, tier = measure_translation_offset_with_tier(before, after)

        assert offset is not None
        assert tier == "fallback_x8"

    def test_reports_none_when_nothing_clears_min_score(self) -> None:
        before = _textured_image((128, 128), seed=4)
        after = _textured_image((128, 128), seed=5)

        offset, tier = measure_translation_offset_with_tier(before, after)

        assert offset is None
        assert tier == "none"

    def test_agrees_with_measure_translation_offset_on_the_offset_itself(self) -> None:
        """Same shared implementation, not a second, potentially-divergent
        code path -- the plain function's answer must always equal the
        tier-reporting variant's own `offset`, for every tier."""
        before = _textured_image((128, 128), seed=1)
        after = np.roll(before, shift=(3, -5), axis=(0, 1))

        plain = measure_translation_offset(before, after)
        with_tier, _tier = measure_translation_offset_with_tier(before, after)

        assert plain == with_tier


def _sparse_star_image(shape: tuple[int, int], seed: int, *, margin: int = 80) -> np.ndarray:
    """Issue #32: a terrestrial-mode scene that's mostly/only stars --
    a handful of Gaussian point sources over a flat background (built on
    `astrotool_core.testing.star_field_image`, the same synthetic generator
    `tests/core/target/test_detector.py` already uses), standing in for a
    real sparse star field the way `_textured_image` stands in for ordinary
    terrestrial content. Deterministically jittered (seeded), 3-6 stars, so
    no two draws are degenerate/symmetric -- a real symmetric star pattern
    is exactly the "ambiguous repeated pattern" issue #32 itself allows the
    estimator to reject, which would make an unlucky fixture flaky rather
    than meaningfully test anything."""
    rng = np.random.default_rng(seed)
    height, width = shape
    star_count = int(rng.integers(3, 7))
    stars = [
        StarSpec(
            x=float(rng.uniform(margin, width - margin)),
            y=float(rng.uniform(margin, height - margin)),
            peak=float(rng.uniform(2000.0, 6000.0)),
            sigma=float(rng.uniform(1.5, 3.0)),
        )
        for _ in range(star_count)
    ]
    return star_field_image(shape, stars, background=100.0)


class TestMaxUnaliasedShiftPx:
    """`max_unaliased_shift_px` (issues #28/#32): the largest `(|dx|, |dy|)`
    `_correlate`'s own unwrap ("fold anything past the frame's midpoint
    back to the equivalent negative shift") reports without aliasing --
    used by `scripts/translation_estimator_benchmark.py` to classify a
    large-shift "wrong displacement" as an expected alias rather than a
    genuine estimator defect, exactly as issue #28's own "Important edge
    consideration" anticipates."""

    def test_matches_half_the_frame_dimensions(self) -> None:
        assert max_unaliased_shift_px((1080, 1920)) == (960, 540)
        assert max_unaliased_shift_px((128, 128)) == (64, 64)

    def test_a_shift_at_the_bound_is_recovered_exactly_not_aliased(self) -> None:
        before = _textured_image((256, 256), seed=11)
        max_dx, max_dy = max_unaliased_shift_px((256, 256))
        after = np.roll(before, shift=(max_dy, max_dx), axis=(0, 1))

        offset = measure_translation_offset(before, after)

        assert offset is not None
        assert offset.dx_px == float(max_dx)
        assert offset.dy_px == float(max_dy)


class TestSparseStarContent:
    """Issue #32: the same one estimator must work when the shared image
    structure is mainly/only stars/point sources, not just extended
    terrestrial texture -- without requiring ASTAP or any external plate
    solver (this whole file never imports one). These tests characterize
    the CURRENT (unmodified) estimator's behavior on sparse content first,
    per both issues' own "don't guess, measure the real envelope"
    instruction -- a third internal strategy (point-source-consensus
    matching) is deliberately NOT added unless this evidence shows it's
    needed.

    `_SHAPE` is deliberately larger than the grid's own 1000px ceiling on
    every side: `_correlate`'s own circular-shift unwrap folds any raw
    shift past half the frame's dimension to its equivalent negative
    (e.g. a true +640 shift on a 1000px-tall frame reads back as -360) --
    a real, expected property of any FFT-correlation approach to a shift
    this large relative to the frame, not a defect (see the module's own
    "Correlating via FFT assumes a circular shift" docstring section) and
    not specific to sparse content. A frame this test's own size (2200px)
    keeps every grid entry within the *unaliased* regime, so these tests
    characterize whether sparse content itself is recoverable -- not
    whether a too-small fixture aliases a large shift. Issue #28's own
    "may expose a valid operating limit rather than a defect" edge
    consideration applies directly to *real* Guide frames (1920x1080,
    where a shift approaching 1000px NECESSARILY exceeds half the
    1080px-tall sensor and will alias for real) -- that is exactly the
    kind of operating-envelope finding `scripts/translation_estimator_
    benchmark.py` (Phase 1b) is meant to surface from real corpus data,
    not something a synthetic fixture should paper over by picking a
    frame size no real sensor has."""

    _SHAPE = (2200, 2200)

    @pytest.mark.parametrize(
        "case", CI_SHIFT_GRID_SUBSET, ids=[case.name for case in CI_SHIFT_GRID_SUBSET]
    )
    def test_known_shift_is_recovered_or_cleanly_rejected_never_wrong(
        self, case: ShiftCase
    ) -> None:
        before = _sparse_star_image(self._SHAPE, seed=1)
        after = np.roll(before, shift=(case.dy, case.dx), axis=(0, 1))

        offset = measure_translation_offset(before, after)

        if offset is None:
            return  # a clean, honest "no usable match" is acceptable
        # A confidently WRONG displacement is never acceptable, at any
        # magnitude or content class (issue #28's explicit taxonomy) --
        # this is the one outcome that must not occur.
        assert offset.dx_px == float(case.dx), case.name
        assert offset.dy_px == float(case.dy), case.name

    def test_a_shift_beyond_the_unaliased_range_reads_back_as_its_own_alias(self) -> None:
        """Synthetic reproduction of evidence found building #32's corpus:
        on a REAL 1920x1080 Guide star frame, applying a (0, +640) shift
        (`max_unaliased_shift_px`'s own dy bound at this shape is 540)
        measured back as exactly (0, -440) -- `640 - 1080 == -440`, the
        mathematically exact circular alias, not noise or a wrong answer
        (see `tests/local_data/test_translation_offset_against_real_
        captures.py`'s star-content shift-grid test for the real-frame
        pin). This is issue #28's own predicted "may expose a valid
        operating limit rather than a defect" for ANY content at a shift
        beyond half the frame, not something specific to sparse content --
        confirms `max_unaliased_shift_px` matches `_correlate`'s own
        unwrap behavior at the exact same shape/shift the real frame
        exposed it at."""
        shape = (1080, 1920)
        base = _sparse_star_image(shape, seed=5)
        _max_dx, max_dy = max_unaliased_shift_px(shape)
        applied_dy = max_dy + 100  # 640, well past the 540 bound

        after = np.roll(base, shift=(applied_dy, 0), axis=(0, 1))
        offset = measure_translation_offset(base, after)

        assert offset is not None
        assert offset.dx_px == 0.0
        assert offset.dy_px == float(applied_dy - shape[0])  # the exact circular alias

    def test_stationary_pair_is_near_zero_high_confidence(self) -> None:
        """Issue #32 Test B's synthetic analogue: two independent noise
        realizations of the same star layout (mirrors
        test_zero_shift_reports_near_zero_offset_and_a_high_score's own
        shape) -- the true displacement is zero by construction."""
        before = _sparse_star_image(self._SHAPE, seed=2)
        after = before.copy()

        offset = measure_translation_offset(before, after)

        assert offset is not None
        assert offset.dx_px == 0.0
        assert offset.dy_px == 0.0
        assert offset.score > 0.9
