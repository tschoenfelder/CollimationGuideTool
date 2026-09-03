"""Guide-scope alignment guidance — issue #29's "help the user align the
Guide scope toward the Main optical axis" product requirement, derived
purely from a `CrossCameraRegistrationResult`'s own geometry (works
equally for a star-field result with zero overlap, per the issue's own
"ASTAP-based star geometry should make this possible even before the
fields overlap").
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from astrotool_core.registration.geometry import fully_contains, polygon_centroid, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import CrossCameraRegistrationResult


@dataclass(frozen=True)
class AlignmentGuidance:
    """`direction_dx`/`direction_dy` are a unit vector in B's own
    image-space (x right, y down) pointing from B's own frame center
    toward A's footprint center -- "move the B scope toward this
    direction" brings B's own boresight toward A's, per the issue's own
    worked example ("Main FOV is left/up of Guide field -> move guide
    scope toward left/up"). `magnitude_px` is the actual centroid-to-
    centroid distance, in B pixels -- 0.0 (and `fully_contained=True`)
    once no further adjustment is needed."""

    fully_contained: bool
    direction_dx: float
    direction_dy: float
    magnitude_px: float
    description: str


def derive_alignment_guidance(
    result: CrossCameraRegistrationResult, prior_b: OpticalPrior
) -> AlignmentGuidance | None:
    """`None` if `result` doesn't carry usable geometry at all (status
    not `.ok`, or -- an `OK_OVERLAP`/`OK_NO_OVERLAP` result should always
    have one, but a caller building a result by hand for a test might
    not) -- there's nothing to guide toward without it."""
    if not result.ok or result.polygon_a_in_b is None:
        return None

    frame_center = (prior_b.sensor_width_px / 2.0, prior_b.sensor_height_px / 2.0)
    frame_b_rect = rect_polygon(
        prior_b.sensor_width_px, prior_b.sensor_height_px, center=frame_center
    )
    a_center = polygon_centroid(result.polygon_a_in_b)

    if fully_contains(frame_b_rect, result.polygon_a_in_b):
        return AlignmentGuidance(
            fully_contained=True,
            direction_dx=0.0,
            direction_dy=0.0,
            magnitude_px=0.0,
            description="Main is fully inside Guide -- no adjustment needed.",
        )

    dx = a_center[0] - frame_center[0]
    dy = a_center[1] - frame_center[1]
    magnitude = math.hypot(dx, dy)
    unit_dx = dx / magnitude if magnitude > 0.0 else 0.0
    unit_dy = dy / magnitude if magnitude > 0.0 else 0.0

    vertical = "down" if dy > 0.0 else "up" if dy < 0.0 else ""
    horizontal = "right" if dx > 0.0 else "left" if dx < 0.0 else ""
    direction_words = (
        " and ".join(word for word in (vertical, horizontal) if word) or "no direction"
    )
    description = (
        f"Main FOV is {direction_words} of Guide's own center -- "
        f"move the guide scope toward {direction_words} (~{magnitude:.0f}px off-center)."
    )
    return AlignmentGuidance(
        fully_contained=False,
        direction_dx=unit_dx,
        direction_dy=unit_dy,
        magnitude_px=magnitude,
        description=description,
    )
