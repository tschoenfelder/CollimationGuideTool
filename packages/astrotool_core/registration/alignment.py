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

from astrotool_core.registration.geometry import (
    Point,
    fully_contains,
    polygon_centroid,
    rect_polygon,
)
from astrotool_core.registration.optical_prior import OpticalPrior, scale_ratio
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)


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


def transform_point_a_to_b(
    point_in_a: Point, prior_a: OpticalPrior, result: CrossCameraRegistrationResult
) -> Point:
    """Project an arbitrary point in optical train A's own pixel space
    into B's, using the same "rotate about A's own center by
    `result.rotation_deg`, scale by `result.scale`, then translate to
    where A's own center actually landed" convention `rect_polygon`/
    `polygon_a_in_b` already use elsewhere in this module and in both
    registrars -- B's own frame center is never assumed; A's transformed
    center is read directly from `polygon_centroid(result.polygon_a_in_b)`
    (issue #15: turning "the star is at (x,y) in Main" into "the star
    should appear near (x',y') in Guide" for guide-camera-assisted
    reacquisition).

    `result.polygon_a_in_b`/`result.rotation_deg`/`result.scale` must all
    be set -- true for any `.ok` result (guaranteed by both registrars)."""
    assert result.polygon_a_in_b is not None
    assert result.rotation_deg is not None
    assert result.scale is not None

    center_b = polygon_centroid(result.polygon_a_in_b)
    local_x = point_in_a[0] - prior_a.sensor_width_px / 2.0
    local_y = point_in_a[1] - prior_a.sensor_height_px / 2.0
    theta = math.radians(result.rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    scale = result.scale
    rotated_x = scale * (cos_t * local_x - sin_t * local_y)
    rotated_y = scale * (sin_t * local_x + cos_t * local_y)
    return (center_b[0] + rotated_x, center_b[1] + rotated_y)


def nominal_registration(
    prior_a: OpticalPrior, prior_b: OpticalPrior
) -> CrossCameraRegistrationResult:
    """A's frame projected centered/unrotated into B's own frame, scaled
    only from each train's own known plate scale -- issue #15's own
    explicitly-allowed "nominal centered FOV relationship... only as an
    explicitly uncalibrated fallback" when no real registration exists
    yet. `confidence=0.0` is deliberate, not merely "unknown" -- a caller
    (e.g. a guide-camera-assisted reacquisition controller) checking
    "is calibration confidence sufficient for automatic mount movement"
    must always refuse on this result, never mistake it for a real,
    confident measurement."""
    scale = scale_ratio(prior_a, prior_b)
    center_b = (prior_b.sensor_width_px / 2.0, prior_b.sensor_height_px / 2.0)
    polygon_a_in_b = rect_polygon(
        prior_a.sensor_width_px * scale, prior_a.sensor_height_px * scale,
        center=center_b, rotation_deg=0.0,
    )
    return CrossCameraRegistrationResult(
        method=RegistrationMethod.TERRESTRIAL,
        status=RegistrationStatus.OK_OVERLAP,
        rotation_deg=0.0,
        scale=scale,
        polygon_a_in_b=polygon_a_in_b,
        confidence=0.0,
        diagnostics={"nominal": True},
    )
