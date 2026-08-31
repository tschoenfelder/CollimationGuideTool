"""Terrestrial-mode displacement measurement: how far a same-camera frame
pair shifted, via phase correlation, for when there's no star (a bright
point source) for `detector.detect_sources()` to lock onto -- e.g. Test
Move exercised indoors/daytime against ordinary terrestrial content
instead of the night sky (see incident 6fa2aa59: "no star detected" is
the correct, deliberate refusal in that case, not a bug -- this module is
the terrestrial alternative `MountTestMovePanel`'s "Star"/"Terrestrial"
toggle switches to instead).

Deliberately a *different*, simpler technique than
`collimation_tool.ui.fov_registration.register_main_frame_in_guide_frame`:
that one matches a *smaller* template against a larger search frame from
a *different* camera, searching over rotation and scale because the two
optical trains aren't co-aligned. Here both frames come from the *same*
camera moments apart (a single ~0.5s pulse in between) -- same
resolution, no rotation, no scale change, translation only -- so this
uses classic FFT phase correlation on the whole frame instead of a
windowed cross-correlation search over candidate angles/scales, and it's
far cheaper (one FFT pair, not dozens of candidates).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

#: Below this, the correlation peak is treated as noise rather than a
#: real match -- two frames with no shared structure (most notably a
#: flat/featureless pair, e.g. a saturated or signal-less capture)
#: correlate near 0 by construction (see measure_translation_offset's
#: "Rejects" section); a real match on genuine shared content reads well
#: above it. Picked with this module's own test suite (synthetic shifted
#: textures score >=0.3; flat/uncorrelated pairs score <1e-3) leaving a
#: wide margin on both sides.
_DEFAULT_MIN_SCORE = 0.05


@dataclass(frozen=True)
class TranslationOffset:
    """How far `after` is shifted relative to `before`, in pixels
    (image-space x-right/y-down, matching `PointSource`'s convention) --
    positive `dx_px` means content moved right, positive `dy_px` means it
    moved down. `score` is the phase-correlation peak height, roughly in
    (0, 1] for a real match -- see `measure_translation_offset`'s
    `min_score`."""

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
    images via FFT phase correlation -- the standard technique behind
    e.g. `skimage.registration.phase_cross_correlation`, implemented here
    in plain numpy so this project doesn't need scikit-image (same
    hand-rolled-over-adding-a-dependency choice as `fov_registration`).

    Whole-pixel precision only (no sub-pixel peak refinement) -- plenty
    for Test Move's purpose (learning which physical axis/direction is
    which), unlike `detect_sources()`'s sub-pixel centroid for star mode;
    a future caller needing sub-pixel terrestrial precision would need to
    add that separately.

    Phase correlation assumes a *circular* shift (content leaving one
    edge reappears at the opposite one); a real camera pan instead
    reveals genuinely new content at the trailing edge. For the small
    shifts this is meant for (a single short pulse, not a large slew)
    that mismatch is a small fraction of the frame and doesn't meaningfully
    move the peak -- the same approximation real stacking/registration
    pipelines make for shifts well under the frame size.

    Returns None if the correlation peak doesn't clear `min_score` --
    either frame lacking enough shared structure to trust (e.g. a flat,
    saturated, or signal-less capture -- exactly incident 6fa2aa59's
    "clipped"/"no_signal" case) or `before`/`after` being unrelated. The
    caller should treat that the same as detect_sources() finding no
    star: don't report a displacement with nothing real behind it.
    """
    if before.shape != after.shape:
        raise ValueError("before and after must be the same shape")
    if before.ndim != 2:
        raise ValueError("before/after must be 2D mono arrays")

    height, width = before.shape
    f1 = np.fft.fft2(before.astype(np.float64))
    f2 = np.fft.fft2(after.astype(np.float64))
    # f2 * conj(f1), not the other way round: the peak of this cross-power
    # spectrum's inverse FFT lands at the forward shift from `before` to
    # `after` (verified empirically against known np.roll() shifts in
    # this module's own tests -- swapping the operands flips every sign).
    cross_power = f2 * np.conj(f1)
    magnitude = np.abs(cross_power)
    normalized = np.divide(
        cross_power, magnitude, out=np.zeros_like(cross_power), where=magnitude > 1e-12
    )
    correlation = np.fft.ifft2(normalized).real
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
