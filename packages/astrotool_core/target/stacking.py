"""Stage 3 quality selection and lucky stacking — issue #17: turn a
sequence of Stage 2's registered short-exposure star frames (issue
#16) into one stable, higher-SNR stacked image for the later radial-
diffraction analysis (#18/#20).

Takes `(pixels, star)` pairs rather than Stage 2's own result type --
the same decoupling precedent Stage 2 itself set by not reusing Stage
1's `resolve_identity` even though it could have imported it.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import ceil

import numpy as np

from astrotool_core.target.point_source import PointSource

#: No measurable width (`fwhm_x`/`fwhm_y` is `None`) -- treated as
#: heavily blurred so the frame scores poorly instead of raising or
#: appearing spuriously sharp.
_FALLBACK_FWHM_PX = 20.0

#: Guards the near-zero-width synthetic case from a divide blow-up.
_MIN_FWHM_PX = 0.5


def score_frame_quality(star: PointSource, background: float) -> float:
    """Higher is better. Combines sharpness (lower FWHM) and signal
    strength above background (higher peak-to-background) into one
    ascending score -- the requirements doc's own text says the exact
    quality formula is an implementation decision; this picks two of
    its five suggested indicators (FWHM, peak-relative-to-background)
    since both are already available on `PointSource` with no new
    detection work."""
    if star.fwhm_x is not None and star.fwhm_y is not None:
        fwhm = max((star.fwhm_x + star.fwhm_y) / 2.0, _MIN_FWHM_PX)
    else:
        fwhm = _FALLBACK_FWHM_PX
    peak_above_background = max(star.peak - background, 0.0)
    return peak_above_background / fwhm


@dataclass(frozen=True)
class StackResult:
    """`stacked` is `None` only if the buffer was empty.
    `quality_scores` covers exactly the frames actually combined."""

    stacked: np.ndarray | None
    frame_count: int
    quality_scores: tuple[float, ...]


@dataclass(frozen=True)
class _BufferedFrame:
    pixels: np.ndarray
    quality: float


class RollingFrameBuffer:
    """Bounded FIFO of the most recently registered usable frames plus
    their measured quality (AC 3.3: "memory usage shall remain
    bounded"). Eviction is structural (`deque(maxlen=capacity)`), not a
    manually checked branch."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self._capacity = capacity
        self._frames: deque[_BufferedFrame] = deque(maxlen=capacity)

    def add(self, pixels: np.ndarray, star: PointSource) -> None:
        """Background is `np.median(pixels)` -- the same established
        fill-value convention as #16's `shift_image` (robust to a
        small bright point source, unlike the mean)."""
        background = float(np.median(pixels))
        quality = score_frame_quality(star, background)
        self._frames.append(_BufferedFrame(pixels=pixels, quality=quality))

    def __len__(self) -> int:
        return len(self._frames)

    def produce_stack(self, *, best_fraction: float | None = None) -> StackResult:
        """`best_fraction=None` (default) combines every buffered
        frame ("all valid registered frames"). A `best_fraction` in
        (0.0, 1.0] keeps only the top `ceil(n * best_fraction)`
        highest-quality frames, at least 1 ("a configurable best
        fraction"). Combines via `np.mean` across the selected frames'
        pixels."""
        if not self._frames:
            return StackResult(stacked=None, frame_count=0, quality_scores=())

        entries = list(self._frames)
        if best_fraction is not None:
            if not 0.0 < best_fraction <= 1.0:
                raise ValueError("best_fraction must be within (0.0, 1.0]")
            keep = max(1, ceil(len(entries) * best_fraction))
            entries = sorted(entries, key=lambda entry: entry.quality, reverse=True)[:keep]

        stacked = np.mean(np.stack([entry.pixels for entry in entries]), axis=0)
        return StackResult(
            stacked=stacked,
            frame_count=len(entries),
            quality_scores=tuple(entry.quality for entry in entries),
        )
