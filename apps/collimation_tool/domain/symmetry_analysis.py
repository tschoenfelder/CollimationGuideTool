"""Screw identification by hand-obstruction shadow.

Ported from smart_telescope's `domain/collimation/processing/obstruction_detection.py`
near-verbatim — pure NumPy, only mechanical change is taking an
astrotool_core `AnalysisPlane` instead of the source's own `ProcessedFrame`.

When the user touches a collimation screw, their finger partially blocks
the incoming light beam and casts a shadow in the defocused star image.
This compares a reference frame (clean donut) to the current frame and
locates the shadow region.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import numpy as np
from astrotool_core.frames.analysis_plane import AnalysisPlane

from collimation_tool.domain.collimation_measurement import estimate_background

_log = logging.getLogger(__name__)

_SHADOW_SIGMA = 5.0
_MIN_SHADOW_PX = 20
_SNR_FULL_CONF = 20.0


@dataclass(frozen=True)
class ObstructionResult:
    """Shadow detected by comparing reference and current frames.

    angle_deg: angle from reference_center to shadow centroid, image
    convention (0deg = +x right, 90deg = +y down).
    """

    shadow_center_x: float
    shadow_center_y: float
    angle_deg: float
    shadow_area_px: int
    confidence: float


def detect_obstruction(
    reference: AnalysisPlane,
    current: AnalysisPlane,
    reference_center_x: float,
    reference_center_y: float,
    shadow_sigma: float = _SHADOW_SIGMA,
    min_shadow_px: int = _MIN_SHADOW_PX,
) -> ObstructionResult | None:
    """Detect the shadow cast by touching a collimation screw.

    Args:
        reference: clean donut plane (captured before touching the screw).
        current: plane captured while a finger is near the screw.
        reference_center_x, reference_center_y: outer ring center (pixels).
        shadow_sigma: threshold multiplier above diff background.
        min_shadow_px: minimum shadow area in pixels.
    """
    ref_data = reference.mono.astype(np.float64)
    cur_data = current.mono.astype(np.float64)
    diff = ref_data - cur_data  # positive where current is darker

    bg_diff, sigma_diff = estimate_background(diff)
    threshold = bg_diff + shadow_sigma * max(sigma_diff, 1.0)

    shadow_mask = diff > threshold
    shadow_area = int(np.sum(shadow_mask))

    _log.debug(
        "detect_obstruction bg_diff=%.1f sigma_diff=%.1f threshold=%.1f area=%d",
        bg_diff,
        sigma_diff,
        threshold,
        shadow_area,
    )

    if shadow_area < min_shadow_px:
        return None

    weights = np.where(shadow_mask, diff - bg_diff, 0.0)
    total = float(weights.sum())
    if total <= 0.0:
        return None

    rows_g = np.arange(reference.height, dtype=np.float64)[:, np.newaxis]
    cols_g = np.arange(reference.width, dtype=np.float64)[np.newaxis, :]
    cy = float((weights * rows_g).sum() / total)
    cx = float((weights * cols_g).sum() / total)

    dx = cx - reference_center_x
    dy = cy - reference_center_y
    angle = math.degrees(math.atan2(dy, dx))

    mean_shadow_diff = float(np.mean(diff[shadow_mask]))
    snr = (mean_shadow_diff - bg_diff) / max(sigma_diff, 1.0)
    confidence = min(1.0, snr / _SNR_FULL_CONF)

    return ObstructionResult(
        shadow_center_x=cx,
        shadow_center_y=cy,
        angle_deg=angle,
        shadow_area_px=shadow_area,
        confidence=confidence,
    )
