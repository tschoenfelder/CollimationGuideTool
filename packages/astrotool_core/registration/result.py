"""CrossCameraRegistrationResult — the one common, algorithm-agnostic
contract both `TerrestrialRegistrar` and `StarFieldRegistrar` (issue #29)
return, so UI/FOV-overlay/alignment-guidance/diagnostics code depends only
on this shape, never on which matcher produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from astrotool_core.registration.geometry import Polygon


class RegistrationMethod(Enum):
    STAR_FIELD = "star_field"
    TERRESTRIAL = "terrestrial"


class RegistrationStatus(Enum):
    """Issue #29's "Failure semantics": a failed (or non-overlapping but
    still valid) registration must say *which* outcome occurred, not
    collapse into one generic "no match". Every status below appears
    explicitly in the issue's own "Star-field outcomes"/"Terrestrial
    failure behavior" sections."""

    #: Valid geometry, non-empty overlap -- the common "it worked" case.
    OK_OVERLAP = "ok_overlap"
    #: Star-field only: both frames solved, but their fields don't
    #: overlap -- issue #29 #4: "NO_OVERLAP is geometry, not necessarily
    #: registration failure" when a real WCS solution backs it.
    OK_NO_OVERLAP = "ok_no_overlap"
    #: Terrestrial only: no meaningful shared image evidence was found at
    #: all -- unlike star-field's OK_NO_OVERLAP, there's no sky-coordinate
    #: truth source to report real "no overlap" geometry from, so this is
    #: "no valid registration", not a geometric fact.
    NO_VALID_REGISTRATION = "no_valid_registration"
    #: The evidence itself (too flat/blurred/saturated, or too little of
    #: it) wasn't usable to match against, regardless of overlap.
    INSUFFICIENT_STRUCTURE = "insufficient_structure"
    #: A repeated/self-similar scene produced more than one materially
    #: distinct candidate close enough in score to be untrustworthy.
    AMBIGUOUS_MATCH = "ambiguous_match"
    #: Star-field: ASTAP isn't installed/configured/reachable at all.
    ASTAP_UNAVAILABLE = "astap_unavailable"
    SOLVE_FAILED_A = "solve_failed_a"
    SOLVE_FAILED_B = "solve_failed_b"
    INSUFFICIENT_STARS = "insufficient_stars"
    INVALID_SOLUTION = "invalid_solution"

    @property
    def ok(self) -> bool:
        return self in (RegistrationStatus.OK_OVERLAP, RegistrationStatus.OK_NO_OVERLAP)


@dataclass(frozen=True)
class CrossCameraRegistrationResult:
    """`polygon_a_in_b`: optical train A's own sensor footprint, expressed
    as a polygon in optical train B's pixel coordinates -- the single
    piece of geometry every other field can be derived from (rotation,
    scale, offset, overlap, alignment guidance). `None` whenever `status`
    isn't `.ok` (there's nothing to report at that point).

    `overlap_polygon`: `polygon_a_in_b` clipped to B's own sensor
    rectangle -- an *empty* tuple (not `None`) for a real, valid
    zero-overlap geometry (`OK_NO_OVERLAP`); `None` only when `status`
    isn't `.ok` at all.
    """

    method: RegistrationMethod
    status: RegistrationStatus
    rotation_deg: float | None = None
    scale: float | None = None  # B-pixels per one A-pixel
    polygon_a_in_b: Polygon | None = None
    overlap_polygon: Polygon | None = None
    confidence: float | None = None
    #: Free-form, method-specific detail (e.g. terrestrial's raw NCC
    #: score/candidate count, star-field's ASTAP exit code/star count) --
    #: never load-bearing for callers, only for diagnostics bundles. See
    #: issue #29's "Diagnostics identify method, optical prior, current
    #: transform, overlap, confidence and failure reason."
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status.ok
