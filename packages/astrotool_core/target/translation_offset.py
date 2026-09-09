"""Terrestrial-mode displacement measurement: how far a same-camera frame
pair shifted, via normalized cross-correlation, for when there's no star
(a bright point source) for `detector.detect_sources()` to lock onto --
e.g. Test Move exercised indoors/daytime against ordinary terrestrial
content instead of the night sky (see incident 6fa2aa59: "no star
detected" is the correct, deliberate refusal in that case, not a bug --
this module is the terrestrial alternative `MountTestMovePanel`'s
"Star"/"Terrestrial" toggle switches to instead).

Deliberately a *different*, simpler technique than
`collimation_tool.ui.fov_registration.register_main_frame_in_guide_frame`:
that one matches a *smaller* template against a larger search frame from
a *different* camera, searching over rotation and scale because the two
optical trains aren't co-aligned. Here both frames come from the *same*
camera moments apart (a single pulse in between) -- same resolution, no
rotation, no scale change, translation only -- so this searches the
whole frame via one FFT pair instead of a windowed search over candidate
angles/scales.

**Not full phase correlation** (an earlier version was): plain
whitened phase correlation (normalizing the cross-power spectrum's
*magnitude* to 1 at every frequency) is the textbook technique behind
e.g. `skimage.registration.phase_cross_correlation`, and works well for
sharp, high-contrast content -- but real incident a4ffe048 found it
badly wrong on real, defocused/low-contrast telescope frames: a pulled
bundle's actual before/after pair (saved via `diagnostic_frames()`, see
`MountTestMovePanel`) had an obvious, large, visually-confirmed shift
between them, yet full phase correlation reported `dx=0, dy=0` at a
score of 0.025 -- completely missing it. Full whitening equalizes every
frequency's amplitude regardless of how little real signal it carries;
for a blurry image (most of its real energy concentrated at low spatial
frequencies), that amplifies high-frequency sensor noise until it
swamps the genuine, weak, low-frequency shift the image actually
contains. This module now computes the (mean-subtracted, unwhitened)
cross-power spectrum directly and normalizes only the *overall* peak by
each frame's own total energy (Cauchy-Schwarz: `sqrt(sum(before^2) *
sum(after^2))`) -- the same normalized cross-correlation coefficient
`cv2.matchTemplate(..., TM_CCOEFF_NORMED)` computes, just evaluated at
every candidate shift at once via FFT instead of a sliding window.
Measured directly against a4ffe048's real pair: this recovers a large,
clearly-correct-looking shift the whitened version missed entirely, at
a score of 0.45 -- and *also* corrected the adjacent camera's answer,
which whitened phase correlation had reported as `dx=0, dy=0` (score
0.049, just barely passing the old threshold) but this method instead
finds `dx=9, dy=0` at score 0.99 -- a materially different, much more
confident answer for content that scored deceptively "fine" before.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Below this, the correlation peak is treated as noise rather than a
#: real match -- two frames with no shared structure correlate near 0 by
#: construction; a real match on genuine shared content reads far above
#: it. Empirically calibrated (see module docstring for the algorithm
#: this replaced and why): *unrelated* synthetic noise pairs (independent
#: per-pixel Gaussian -- the adversarial case; two genuinely different
#: real photos should separate even more cleanly) score up to ~0.0385
#: across 8 sampled seed pairs. Every genuine real-camera match measured
#: so far -- including a badly defocused frame that scored just 0.03
#: under the old whitened-phase-correlation metric -- scores at least
#: 0.45 under this one. 0.15 sits with a wide margin on both sides (>3x
#: the unrelated-noise ceiling, well under a third of the lowest observed
#: real match) rather than the old metric's cramped 0.03-0.07 range that
#: caused incident a082144a in the first place. See
#: tests/core/target/test_translation_offset.py for the regression
#: coverage on both sides of this gap.
_DEFAULT_MIN_SCORE = 0.15

#: Same 0.1%-of-max tolerance auto_exposure.py's own saturated-pixel
#: floor already uses (`pixels >= actual_max * 0.999`) -- not a hard
#: bit-depth-specific clip value (this project handles multiple sensors/
#: bit depths), just "how close to this frame's own observed ceiling".
_NEAR_MAX_RELATIVE_TOLERANCE = 0.999

#: Above this fraction of a frame's pixels sitting at its own near-max
#: value, treat it as too saturated to trust this module's unwhitened
#: whole-frame correlation. Real incident
#: ef49ecb1-a052-44ea-b45e-01145a9f0c33: a real guide-camera terrestrial
#: Test Move pair, ~12-26% near-max-saturated (a large blown-out sky
#: region), collapsed BOTH of two independent, roughly-orthogonal real
#: mount-axis pulses to an identical dx_px=0.0 at confidently-high scores
#: (0.98, 0.71, both above _DEFAULT_MIN_SCORE) -- a large, near-uniform
#: saturated region dominates the whole-frame FFT correlation with a
#: broad, shallow, near-parabolic zero-lag-centered ridge (score barely
#: dropping from 0.983 at dx=0 to 0.951 at dx=+/-10) that swamps the
#: real, sharper shift signal in the remaining unsaturated content;
#: is_degenerate() only caught the resulting calibration by coincidence
#: (two supposedly-orthogonal axes both reading (0, 0)), not because this
#: module noticed anything wrong. Every other real frame measured against
#: every real dataset in this repo -- Main's own frames from the SAME run
#: (0.0-0.48%), the star-mode calibration_dataset_2026-09-02 set (12
#: files, all 0.0%), and the fov_registration out_of_focus_daytime set
#: (0.0% both) -- reads at most 0.48%. 0.05 (5%) sits with a wide margin
#: on both sides, the same real-incident-vs-real-legitimate-value
#: methodology _DEFAULT_MIN_SCORE above and terrestrial_registrar.py's
#: own _DEFAULT_MIN_RELATIVE_CONTRAST/_DEFAULT_MIN_SHARPNESS_RATIO guards
#: use (a *different* algorithm, not reused directly -- see this module's
#: own "deliberately a different, simpler technique" docstring section).
_DEFAULT_MAX_SATURATED_FRACTION = 0.05


def _near_max_fraction(frame: np.ndarray) -> float:
    """Fraction of `frame`'s pixels within 0.1% of its own observed
    maximum -- bit-depth-agnostic (only ever compares a frame to itself),
    matching auto_exposure.py's own `pixels >= actual_max * 0.999` idiom."""
    actual_max = float(frame.max())
    if actual_max <= 0.0:
        return 0.0
    return float(np.mean(frame >= actual_max * _NEAR_MAX_RELATIVE_TOLERANCE))


#: Box-downsample factor tried as a fallback when the full-resolution
#: correlation doesn't clear `min_score` -- real incident
#: 93ba361f-18c6-46f6-9a53-fd05be821b01: a real terrestrial Main-camera
#: frame pair with heavy per-pixel sensor noise (visually confirmed real
#: structure present -- diagonal cable/branch-like streaks, clearly
#: visible to a human eye in the diagnostic's own display image) scored
#: only 0.098/0.093 at full resolution for its two real axis pulses, both
#: well below `_DEFAULT_MIN_SCORE` -- because independent per-pixel
#: sensor noise dominates this module's own whole-frame energy
#: normalization (this module is deliberately unwhitened -- see the
#: module docstring -- normalized by *total* frame energy, noise
#: included). Reconstructing that real pair's own correlation surface at
#: several box-downsample factors recovered a strong, mutually consistent
#: peak from x4 upward (score climbing 0.098 -> 0.57 -> 0.75 -> 0.82 at
#: x1/x4/x8/x32, converging on the same dx/dy at every one of them) --
#: averaging NxN blocks together cancels independent per-pixel noise
#: (variance shrinks ~1/N per block) while preserving the real, broadband
#: but lower-frequency structure a genuine scene has, something full
#: resolution alone completely missed here.
#:
#: 8 was chosen, not a larger factor, because the false-positive floor
#: for *unrelated* content rises as the downsampled image shrinks (fewer
#: independent correlation samples -- verified empirically: two
#: independent-noise 1920x1080 frames, this app's own smallest real
#: sensor, score up to only 0.026 at x8 vs. up to 0.08 already at x32).
#: At x8, both this incident's real axis pairs (0.75, 0.53) sit with a
#: wide margin above `_DEFAULT_MIN_SCORE` while that unrelated-content
#: ceiling (0.026) sits with an equally wide margin below it -- the same
#: two-sided-margin methodology `_DEFAULT_MIN_SCORE`/
#: `_DEFAULT_MAX_SATURATED_FRACTION` above already use.
_FALLBACK_DOWNSAMPLE_FACTOR = 8

#: Below this many pixels per side, skip the downsample fallback
#: entirely rather than risk a false-positive match -- the unrelated-
#: content noise floor above was only verified down to real-camera-scale
#: frames (this app's smallest sensor is 1920x1080); a synthetic fixture
#: smaller than this (e.g. this file's own small unit-test images) simply
#: never exercises the fallback, by design, rather than needing its own
#: separately-recalibrated threshold.
_FALLBACK_MIN_FRAME_SIDE_PX = 256

#: A second, coarser factor used only to VALIDATE the primary
#: `_FALLBACK_DOWNSAMPLE_FACTOR` result -- never to compute an answer
#: itself. Real incident 6cb859d2-7a94-4e44-8aff-585f0bf2466b, the very
#: next real pair to hit the x8 fallback after it shipped: a genuinely
#: featureless/hazy real Main-camera pair (full-resolution score 0.036,
#: barely above the pure-unrelated-noise floor for this sensor size --
#: roughly 20x lower than 93ba361f's own real 0.098/0.093) produced a
#: CONFIDENT but spurious (dx=0, dy=0) match at x8 (score 0.62-0.63)
#: purely because there wasn't enough real correlated structure to find,
#: once nearly everything but a broad, low-frequency-similar gradient got
#: averaged away. The tell: pushed further to x16/x32/x64, this pair's
#: score kept climbing steeply toward 1.0 (0.62 -> 0.87 -> 0.96 -> 0.99)
#: -- exactly what *any* sufficiently downsampled pair eventually does
#: once there are too few independent samples left to discriminate a
#: real match from an accidental one (a genuine peak and pure coincidence
#: converge as degrees of freedom run out). A trustworthy match instead
#: plateaus quickly: 93ba361f's own real, already-verified-correct
#: recovery only grew ~8-9% from x8 to x16 (0.75->0.82, 0.53->0.57)
#: before flattening out through x128, while this incident's spurious
#: match grew ~38-40% over that same single octave. See
#: `_FALLBACK_MAX_SCORE_GROWTH_RATIO`.
_FALLBACK_VALIDATION_FACTOR = 2 * _FALLBACK_DOWNSAMPLE_FACTOR

#: Above this much relative growth in score between
#: `_FALLBACK_DOWNSAMPLE_FACTOR` and `_FALLBACK_VALIDATION_FACTOR`, treat
#: the x8 match as still-climbing noise-floor artifact rather than a
#: plateaued, trustworthy peak -- see that constant's own docstring for
#: the real evidence: ~1.08-1.09 for two real genuine recoveries (plus
#: ~0.90-1.00 for synthetic reconstructions of both a real match and
#: genuinely unrelated content, both already excluded by `min_score`
#: itself) vs. ~1.38-1.39 for two real spurious "matches" from the same
#: incident. 1.25 sits with a comfortable, roughly symmetric margin
#: between the two.
_FALLBACK_MAX_SCORE_GROWTH_RATIO = 1.25

#: Once the x8 fallback's own coarse match is validated as trustworthy
#: (cleared `min_score` and the plateau check above), the *full-
#: resolution* correlation's own peak -- already computed by the caller
#: to decide whether the fallback was even needed -- usually still sits
#: right at the real shift; it just wasn't confident enough (too much
#: independent noise contributing to the whole-frame energy
#: normalization) to trust on its own. Preferring that precise location
#: over the coarse `x * _FALLBACK_DOWNSAMPLE_FACTOR` position recovers
#: this module's usual whole-pixel precision instead of only
#: `_FALLBACK_DOWNSAMPLE_FACTOR`-pixel granularity, in the common case
#: where the two agree. Verified against 93ba361f's own two real,
#: already-validated recoveries: axis1's full-resolution peak matched
#: the coarse x8 position *exactly* (0px difference); axis2's differed
#: by (11, 10)px -- both real cases fit comfortably inside twice the
#: downsample factor, which is what this tolerance uses. A full-
#: resolution peak *outside* this tolerance is discarded in favor of the
#: coarse (already-validated) position instead -- it most likely reflects
#: noise dominating the full-resolution peak search rather than the real
#: shift the coarse, noise-suppressed pass found.
_FULL_RES_AGREEMENT_TOLERANCE_PX = 2 * _FALLBACK_DOWNSAMPLE_FACTOR


def _correlate(before: np.ndarray, after: np.ndarray) -> tuple[float, float, float] | None:
    """The shared normalized-cross-correlation core -- mean-subtract,
    energy-normalize, FFT-correlate, find the peak, unwrap it to a
    (dx, dy) shift in `before`/`after`'s own pixel units. `None` if
    either frame has zero variance (nothing to normalize by -- e.g. a
    flat/saturated capture, incident 6fa2aa59). Factored out of
    `measure_translation_offset` so the downsample fallback below can
    reuse the exact same math at a reduced resolution instead of
    duplicating it -- see that function's own docstring for why
    unwhitened, energy-normalized correlation is used at all."""
    height, width = before.shape
    b = before.astype(np.float64) - before.mean()
    a = after.astype(np.float64) - after.mean()
    energy_norm = np.sqrt(np.sum(b * b) * np.sum(a * a))
    if energy_norm <= 0.0:
        return None
    f1 = np.fft.fft2(b)
    f2 = np.fft.fft2(a)
    # f2 * conj(f1), not the other way round -- see
    # measure_translation_offset's own comment on this same line.
    correlation = np.fft.ifft2(f2 * np.conj(f1)).real / energy_norm
    peak_row, peak_col = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    score = float(correlation[peak_row, peak_col])
    dy = float(peak_row if peak_row <= height // 2 else peak_row - height)
    dx = float(peak_col if peak_col <= width // 2 else peak_col - width)
    return dx, dy, score


def _box_downsample(frame: np.ndarray, factor: int) -> np.ndarray:
    """Average non-overlapping `factor`x`factor` blocks together -- a box
    low-pass filter, cheap and dependency-free (matches this project's
    own hand-rolled-over-adding-a-dependency choice elsewhere). Cropped
    to the largest multiple of `factor` in each dimension first; real
    camera frames are always far larger than a single block, so a few
    discarded trailing rows/columns never meaningfully affects the
    result."""
    height, width = frame.shape
    height2 = height - height % factor
    width2 = width - width % factor
    cropped = frame[:height2, :width2].astype(np.float64)
    return cropped.reshape(height2 // factor, factor, width2 // factor, factor).mean(axis=(1, 3))


def _fallback_downsampled_match(
    before: np.ndarray,
    after: np.ndarray,
    min_score: float,
    *,
    full_res_offset: tuple[float, float] | None,
) -> TranslationOffset | None:
    """Retries the correlation at `_FALLBACK_DOWNSAMPLE_FACTOR`x reduced
    resolution -- see that constant's own docstring for the real incident
    and evidence behind this. Only called once the full-resolution
    correlation has already failed to clear `min_score`; returns `None`
    (not a partial/best-effort guess) if the downsampled attempt doesn't
    clear it either, or the frame is too small for this fallback to be
    trustworthy (`_FALLBACK_MIN_FRAME_SIDE_PX`).

    `full_res_offset` is the full-resolution `_correlate()` peak the
    caller already computed (`None` only if that frame pair had zero
    variance) -- see `_FULL_RES_AGREEMENT_TOLERANCE_PX`'s own docstring:
    once the coarse match here is validated, this is used to recover
    whole-pixel precision instead of only `_FALLBACK_DOWNSAMPLE_FACTOR`-
    pixel granularity whenever the two agree closely enough.

    Real incident 6cb859d2-7a94-4e44-8aff-585f0bf2466b: `min_score`
    clearing the x8 attempt alone isn't sufficient -- also validated
    against a second, coarser attempt (`_FALLBACK_VALIDATION_FACTOR`)
    before trusting it; see that constant's own docstring for why (a
    still-climbing-toward-1.0 score between the two is a spurious,
    too-few-degrees-of-freedom artifact, not a genuine plateaued match)."""
    height, width = before.shape
    if height < _FALLBACK_MIN_FRAME_SIDE_PX or width < _FALLBACK_MIN_FRAME_SIDE_PX:
        return None
    factor = _FALLBACK_DOWNSAMPLE_FACTOR
    down_before = _box_downsample(before, factor)
    down_after = _box_downsample(after, factor)
    # Reusing _correlate's own zero-variance guard here is defensive
    # rather than something realistic input can trigger in practice: a
    # box-averaged block only comes out perfectly, bit-exactly flat if
    # the *original* full-resolution content already was (already
    # excluded above by the saturation guard, or by measure_translation_
    # offset's own earlier full-resolution _correlate call returning None
    # first) -- floating-point rounding in np.ndarray.mean() alone
    # already keeps a genuinely varying input from averaging down to an
    # exact constant (confirmed empirically: even literal bit-for-bit
    # identical repeated blocks left ~1e-13 residual variance after
    # subtracting the block mean back out).
    result = _correlate(down_before, down_after)
    if result is None:
        return None
    dx, dy, score = result
    if score < min_score:
        return None

    validation_factor = _FALLBACK_VALIDATION_FACTOR
    validation_result = _correlate(
        _box_downsample(before, validation_factor), _box_downsample(after, validation_factor)
    )
    if validation_result is None:
        return None
    _, _, validation_score = validation_result
    if validation_score > score * _FALLBACK_MAX_SCORE_GROWTH_RATIO:
        return None

    if full_res_offset is not None:
        full_dx, full_dy = full_res_offset
        if (
            abs(full_dx - dx * factor) <= _FULL_RES_AGREEMENT_TOLERANCE_PX
            and abs(full_dy - dy * factor) <= _FULL_RES_AGREEMENT_TOLERANCE_PX
        ):
            return TranslationOffset(dx_px=full_dx, dy_px=full_dy, score=score)

    return TranslationOffset(dx_px=dx * factor, dy_px=dy * factor, score=score)


@dataclass(frozen=True)
class TranslationOffset:
    """How far `after` is shifted relative to `before`, in pixels
    (image-space x-right/y-down, matching `PointSource`'s convention) --
    positive `dx_px` means content moved right, positive `dy_px` means it
    moved down. `score` is the normalized cross-correlation coefficient
    at the peak, in (0, 1] for a real match (1.0 is a perfect linear
    match) -- see `measure_translation_offset`'s `min_score`."""

    dx_px: float
    dy_px: float
    score: float


def measure_translation_offset(
    before: np.ndarray,
    after: np.ndarray,
    *,
    min_score: float = _DEFAULT_MIN_SCORE,
    max_saturated_fraction: float = _DEFAULT_MAX_SATURATED_FRACTION,
) -> TranslationOffset | None:
    """Estimate the whole-frame translation between two same-shape mono
    images via FFT-evaluated normalized cross-correlation -- see this
    module's own docstring for why it isn't full phase correlation
    (`skimage.registration.phase_cross_correlation`'s technique) despite
    starting from the same FFT cross-power spectrum; implemented here in
    plain numpy so this project doesn't need scikit-image/OpenCV (same
    hand-rolled-over-adding-a-dependency choice as `fov_registration`).

    Whole-pixel precision only (no sub-pixel peak refinement) -- plenty
    for Test Move's purpose (learning which physical axis/direction is
    which), unlike `detect_sources()`'s sub-pixel centroid for star mode;
    a future caller needing sub-pixel terrestrial precision would need to
    add that separately.

    Correlating via FFT assumes a *circular* shift (content leaving one
    edge reappears at the opposite one); a real camera pan instead
    reveals genuinely new content at the trailing edge. For the small
    shifts this is meant for (a single short pulse, not a large slew)
    that mismatch is a small fraction of the frame and doesn't meaningfully
    move the peak -- the same approximation real stacking/registration
    pipelines make for shifts well under the frame size.

    Returns None if either frame has zero variance (perfectly flat --
    e.g. a saturated or signal-less capture, exactly incident 6fa2aa59's
    "clipped"/"no_signal" case; there is nothing to normalize by), if
    either frame is more than `max_saturated_fraction` saturated (real
    incident ef49ecb1 -- see `_DEFAULT_MAX_SATURATED_FRACTION`'s own
    docstring: a large *partially* saturated region, unlike a fully flat
    frame, has nonzero variance and would otherwise sail through this
    check while still confidently misreporting the shift), or neither the
    full-resolution correlation peak nor a reduced-resolution fallback
    attempt (real incident 93ba361f -- see `_FALLBACK_DOWNSAMPLE_FACTOR`'s
    own docstring: independent per-pixel sensor noise can swamp a real
    shift at full resolution alone) clears `min_score` (not enough shared
    structure to trust even once that noise is averaged down, or
    `before`/`after` genuinely unrelated). The caller should treat any of
    these the same as detect_sources() finding no star: don't report a
    displacement with nothing real behind it. A `TranslationOffset`
    returned via the fallback is usually still at this module's normal
    whole-pixel precision -- see `_FULL_RES_AGREEMENT_TOLERANCE_PX`'s own
    docstring: once the coarse match is validated, the full-resolution
    peak's own location is preferred whenever it agrees closely enough,
    falling back to only `_FALLBACK_DOWNSAMPLE_FACTOR`-pixel granularity
    when it doesn't.
    """
    if before.shape != after.shape:
        raise ValueError("before and after must be the same shape")
    if before.ndim != 2:
        raise ValueError("before/after must be 2D mono arrays")

    if (
        _near_max_fraction(before) > max_saturated_fraction
        or _near_max_fraction(after) > max_saturated_fraction
    ):
        return None

    # Mean-subtracted so a brightness/exposure difference between the two
    # captures doesn't bias the match toward wherever the frame happens
    # to be brightest -- standard normalized-cross-correlation practice.
    # f2 * conj(f1), not the other way round: the peak of this cross-power
    # spectrum's inverse FFT lands at the forward shift from `before` to
    # `after` (verified empirically against known np.roll() shifts in
    # this module's own tests -- swapping the operands flips every sign).
    # Deliberately *not* whitened (no division by |cross_power| here) --
    # see module docstring for why full phase correlation's per-frequency
    # magnitude normalization badly mismeasures real, defocused/low-
    # contrast telescope frames (incident a4ffe048); dividing the whole
    # correlation surface by both frames' own total energy afterward is
    # the standard normalized-cross-correlation-coefficient normalization
    # instead, applied once rather than per frequency bin. See
    # `_correlate`'s own docstring -- shared with the downsample fallback
    # below.
    result = _correlate(before, after)
    if result is None:
        return None
    dx, dy, score = result
    if score < min_score:
        # Real incident 93ba361f: before giving up outright, retry at a
        # reduced resolution -- see `_FALLBACK_DOWNSAMPLE_FACTOR`'s own
        # docstring for why (independent per-pixel sensor noise can
        # swamp a real shift's contribution to this module's own
        # whole-frame energy normalization at full resolution, even
        # though the shift is still clearly recoverable once that noise
        # is averaged down).
        return _fallback_downsampled_match(before, after, min_score, full_res_offset=(dx, dy))

    # The peak position wraps around at the frame edges (a shift of -1
    # looks identical to a shift of height-1 under a circular assumption)
    # -- fold anything past the frame's midpoint back to the equivalent
    # negative shift. Already done inside `_correlate` itself, so `dx`/
    # `dy` here are already unwrapped.
    return TranslationOffset(dx_px=dx, dy_px=dy, score=score)
