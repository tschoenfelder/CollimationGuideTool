"""roi_selector — initial ROI/lock-point selection from a single-frame
detection result.

New: no existing analog. A single-frame heuristic rather than a literal
multi-frame "persistent" check (which would need track_sources/
classify_temporal_tracks over a rolling window before any lock target
exists at all) — pick the brightest non-donut-like source, preferring
``detector.py``'s "normal_star" classification so a saturated blob or a
distorted/defocused source is only chosen when nothing better is present.
"""

from __future__ import annotations

from astrotool_core.target.detector import DetectionResult
from astrotool_core.target.point_source import PointSource

_PREFERRED_KIND = "normal_star"


def select_target(detection: DetectionResult) -> PointSource | None:
    """Pick the best initial lock target from a DetectionResult, or None."""
    candidates = [source for source in detection.sources if not source.donut_like]
    if not candidates:
        return None

    preferred = [source for source in candidates if source.kind == _PREFERRED_KIND]
    pool = preferred or candidates
    return max(pool, key=lambda source: source.peak)
