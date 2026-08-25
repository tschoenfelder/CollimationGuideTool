"""AnalysisPlane — normalized single-plane view of a Frame for algorithms.

Ported from smart_telescope's
``domain.collimation.processing.frame.ProcessedFrame``/``normalize_frame``.
Any Bayer demosaicing must happen first (see ``pixel_format.py``); this
module treats its input pixel array as a single mono plane.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astrotool_core.frames.frame import Frame


@dataclass(frozen=True)
class AnalysisPlane:
    """Normalized single-frame plane that detection/measurement code consumes.

    raw       : uint16 pixel data, shape (height, width). Values are
                clamped from the float32 source.
    mono      : float32 grayscale copy, same shape, same values. Range is
                [0, 2**bit_depth - 1] (not normalized to [0, 1]).
    bit_depth : sensor bit depth (8 or 16).
    width, height : plane dimensions in pixels.
    timestamp : capture timestamp, copied from the source Frame.

    Use the ``normalized`` property to obtain a [0, 1] float32 view.
    """

    raw: np.ndarray
    mono: np.ndarray
    bit_depth: int
    width: int
    height: int
    timestamp: float

    @property
    def normalized(self) -> np.ndarray:
        """Return a float32 array normalized to [0, 1]."""
        max_val = float(2**self.bit_depth - 1)
        return self.mono / max_val


def build_analysis_plane(frame: Frame, *, plane: np.ndarray | None = None) -> AnalysisPlane:
    """Build an AnalysisPlane from a Frame.

    Args:
        frame: source Frame (pixels used unless ``plane`` is given).
        plane: pre-demosaiced mono plane to use instead of ``frame.pixels``
            (pass this for a demosaiced color-sensor channel).

    Returns:
        AnalysisPlane with independent copies of the pixel data.
    """
    pix = frame.pixels if plane is None else plane
    # own copy in the already-float32 branch — do not alias the source buffer
    pix = pix.astype(np.float32) if pix.dtype != np.float32 else pix.copy()

    bit_depth = frame.bit_depth
    raw = np.clip(pix, 0.0, float(2**bit_depth - 1)).astype(np.uint16)

    height, width = pix.shape
    return AnalysisPlane(
        raw=raw,
        mono=pix,
        bit_depth=bit_depth,
        width=int(width),
        height=int(height),
        timestamp=frame.timestamp,
    )
