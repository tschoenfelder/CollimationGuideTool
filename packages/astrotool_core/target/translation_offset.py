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
    check while still confidently misreporting the shift), or the
    correlation peak doesn't clear `min_score` (not enough shared
    structure to trust, or `before`/`after` genuinely unrelated). The
    caller should treat any of these the same as detect_sources() finding
    no star: don't report a displacement with nothing real behind it.
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

    height, width = before.shape
    # Mean-subtracted so a brightness/exposure difference between the two
    # captures doesn't bias the match toward wherever the frame happens
    # to be brightest -- standard normalized-cross-correlation practice.
    b = before.astype(np.float64) - before.mean()
    a = after.astype(np.float64) - after.mean()
    energy_norm = np.sqrt(np.sum(b * b) * np.sum(a * a))
    if energy_norm <= 0.0:
        return None

    f1 = np.fft.fft2(b)
    f2 = np.fft.fft2(a)
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
    # instead, applied once rather than per frequency bin.
    correlation = np.fft.ifft2(f2 * np.conj(f1)).real / energy_norm
    peak_row, peak_col = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    score = float(correlation[peak_row, peak_col])
    if score < min_score:
        return None

    # The peak position wraps around at the frame edges (a shift of -1
    # looks identical to a shift of height-1 under a circular assumption)
    # -- fold anything past the frame's midpoint back to the equivalent
    # negative shift.
    dy = float(peak_row if peak_row <= height // 2 else peak_row - height)
    dx = float(peak_col if peak_col <= width // 2 else peak_col - width)
    return TranslationOffset(dx_px=dx, dy_px=dy, score=score)
