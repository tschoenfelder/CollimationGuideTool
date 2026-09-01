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
    "clipped"/"no_signal" case; there is nothing to normalize by) or the
    correlation peak doesn't clear `min_score` (not enough shared
    structure to trust, or `before`/`after` genuinely unrelated). The
    caller should treat either case the same as detect_sources() finding
    no star: don't report a displacement with nothing real behind it.
    """
    if before.shape != after.shape:
        raise ValueError("before and after must be the same shape")
    if before.ndim != 2:
        raise ValueError("before/after must be 2D mono arrays")

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
