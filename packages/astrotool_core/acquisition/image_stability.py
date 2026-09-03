"""Image-stability verification — issue #30: "Is the image stable enough
to take a calibration measurement now?", a different question from the
production translation estimator's "how far did the image move between
two already-valid calibration frames?" (`measure_translation_offset`).

This module answers the first question by measuring frame-to-frame
displacement across a short consecutive sequence, reusing
`measure_translation_offset` as a low-level primitive (issue #30's own
"different responsibilities even if they happen to share low-level
image-processing helpers") — never called from the calibration/
measurement code path itself, only from
`astrotool_core.acquisition.motion_aware_acquisition`'s own settling
loop.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import numpy as np

from astrotool_core.target.translation_offset import measure_translation_offset


class StabilityStatus(Enum):
    #: Every consecutive pair in the sequence measured within tolerance.
    STABLE = "stable"
    #: At least one consecutive pair moved beyond tolerance.
    UNSTABLE = "unstable"
    #: Fewer than two frames were given -- nothing to compare.
    INSUFFICIENT_SAMPLES = "insufficient_samples"
    #: A consecutive pair had no correlatable content at all (e.g. one
    #: frame badly blurred/saturated) -- issue #30's own "Do not silently
    #: accept a blurred or moving frame merely because a timeout
    #: elapsed": treated as *not* proven stable, the conservative
    #: direction, rather than skipped/ignored.
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class StabilityCheckResult:
    status: StabilityStatus
    max_displacement_px: float | None
    samples_checked: int

    @property
    def stable(self) -> bool:
        return self.status is StabilityStatus.STABLE


def check_image_stability(
    frames: Sequence[np.ndarray], *, tolerance_px: float, min_samples: int = 2
) -> StabilityCheckResult:
    """`frames` in delivery order (oldest first) — every *consecutive*
    pair's own displacement (via `measure_translation_offset`) must fall
    within `tolerance_px` for the whole sequence to be `STABLE`.
    Consecutive-pair, not every-pair-against-the-first: this catches
    genuine unsteady drift/vibration across the sequence, not just net
    displacement between the first and last sample (a mount oscillating
    back toward its starting position would otherwise read as stable).

    `min_samples` (default 2, the minimum meaningful comparison) lets a
    caller require a longer run of agreement before trusting stability —
    issue #30 #2: "One good-looking frame after motion may be
    accidental."
    """
    if len(frames) < max(2, min_samples):
        return StabilityCheckResult(StabilityStatus.INSUFFICIENT_SAMPLES, None, len(frames))

    max_displacement = 0.0
    for before, after in zip(frames, frames[1:], strict=False):
        offset = measure_translation_offset(before, after)
        if offset is None:
            return StabilityCheckResult(StabilityStatus.INDETERMINATE, None, len(frames))
        displacement = math.hypot(offset.dx_px, offset.dy_px)
        max_displacement = max(max_displacement, displacement)

    status = (
        StabilityStatus.STABLE if max_displacement <= tolerance_px else StabilityStatus.UNSTABLE
    )
    return StabilityCheckResult(status, max_displacement, len(frames))
