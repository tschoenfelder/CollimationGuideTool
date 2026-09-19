"""ArtificialStarRegistrar — issue #37: a third cross-camera registration
matcher for the artificial-star / single-isolated-point-source case
(daytime/cloudy collimation testing), behind the SAME shared
`CrossCameraRegistrationResult` contract `TerrestrialRegistrar` and
`StarFieldRegistrar` (issue #29) already return -- the existing
FOV-overlay/alignment-guidance UI path works for this mode unchanged.

Unlike Terrestrial (shared texture) or Star-field (many astronomical
stars, ASTAP/WCS), a single point gives only a 2-DOF translation --
rotation is never measurable from one point, and scale is only known
from each optical train's own configured plate scale, never searched.
`assumed_rotation_deg` is therefore an explicit, documented PRIOR
(default 0.0, cameras assumed mechanically co-aligned), not something
this registrar derives -- the same "translation+scale from priors,
rotation as an explicit assumption" precedent `alignment.
nominal_registration` already established, just refined here with a
real single-point-measured translation instead of a centered guess.

Deliberately takes in-memory arrays with a no-arg constructor (like
`TerrestrialRegistrar`, unlike `StarFieldRegistrar`) -- structurally
cannot depend on ASTAP.

Convention: `frame_a`/`prior_a` = the smaller-FOV frame (Main) the
artificial star is selected from; `frame_b`/`prior_b` = the larger-FOV
frame (Guide) the corresponding point is searched for and marked in --
matches this codebase's own existing Main=A/Guide=B convention (see
`main_window.py`'s `_on_calibrate_fov`).
"""

from __future__ import annotations

import math

import numpy as np

from astrotool_core.registration.alignment import transform_point_a_to_b
from astrotool_core.registration.geometry import overlap_polygon, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior, scale_ratio
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.point_source import PointSource

#: A candidate this close to any frame edge is rejected -- an
#: implementation decision (the issue gives no exact number), mirroring
#: `collimation_measurement.detect_clipping`'s own margin-based
#: approach for a different (ring-fit) use case.
_EDGE_MARGIN_PX = 10.0

#: Search radius (in B/larger-FOV pixels) around the predicted position
#: within which a Guide candidate is considered a plausible match -- a
#: fraction of Guide's own smaller sensor dimension. An implementation
#: decision, generous enough to tolerate real-world Main/Guide
#: misalignment (the whole point of this mode is to help fix that),
#: tight enough to still reject an unrelated far-away source.
_MATCH_TOLERANCE_FRACTION = 0.35


def _failure(status: RegistrationStatus, **diagnostics: object) -> CrossCameraRegistrationResult:
    return CrossCameraRegistrationResult(
        method=RegistrationMethod.ARTIFICIAL_STAR, status=status, diagnostics=diagnostics
    )


def _filter_usable(
    sources: tuple[PointSource, ...], *, image_shape: tuple[int, int]
) -> list[PointSource]:
    """Non-donut, non-saturated, not within `_EDGE_MARGIN_PX` of any
    edge -- `select_target`'s own brightest-non-donut heuristic doesn't
    reject saturated sources and has no "how many candidates" concept
    at all, so this mode needs its own filter rather than reusing it."""
    height, width = image_shape
    return [
        source
        for source in sources
        if not source.donut_like
        and not source.saturated
        and _EDGE_MARGIN_PX <= source.x <= width - _EDGE_MARGIN_PX
        and _EDGE_MARGIN_PX <= source.y <= height - _EDGE_MARGIN_PX
    ]


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class ArtificialStarRegistrar:
    def register(
        self,
        frame_a: np.ndarray,
        frame_b: np.ndarray,
        prior_a: OpticalPrior,
        prior_b: OpticalPrior,
        *,
        assumed_rotation_deg: float = 0.0,
    ) -> CrossCameraRegistrationResult:
        # The "smaller-FOV nested in larger-FOV" premise this mode
        # depends on must actually hold in the configured priors --
        # checked before any detection work, regardless of frame content.
        if (
            prior_a.fov_width_arcsec >= prior_b.fov_width_arcsec
            or prior_a.fov_height_arcsec >= prior_b.fov_height_arcsec
        ):
            return _failure(
                RegistrationStatus.INSUFFICIENT_PRIOR,
                prior_a_fov_arcsec=(prior_a.fov_width_arcsec, prior_a.fov_height_arcsec),
                prior_b_fov_arcsec=(prior_b.fov_width_arcsec, prior_b.fov_height_arcsec),
            )

        candidates_a = _filter_usable(
            detect_sources(frame_a).sources, image_shape=frame_a.shape
        )
        if len(candidates_a) != 1:
            return _failure(
                RegistrationStatus.INSUFFICIENT_STARS,
                candidate_count_a=len(candidates_a),
                candidates_a=[(source.x, source.y) for source in candidates_a],
            )
        reference = candidates_a[0]
        reference_point = (reference.x, reference.y)

        scale = scale_ratio(prior_a, prior_b)
        center_b = (prior_b.sensor_width_px / 2.0, prior_b.sensor_height_px / 2.0)
        nominal_polygon = rect_polygon(
            prior_a.sensor_width_px * scale,
            prior_a.sensor_height_px * scale,
            center=center_b,
            rotation_deg=assumed_rotation_deg,
        )
        nominal_result = CrossCameraRegistrationResult(
            method=RegistrationMethod.ARTIFICIAL_STAR,
            status=RegistrationStatus.OK_OVERLAP,
            rotation_deg=assumed_rotation_deg,
            scale=scale,
            polygon_a_in_b=nominal_polygon,
        )
        predicted_point = transform_point_a_to_b(reference_point, prior_a, nominal_result)

        match_tolerance_px = _MATCH_TOLERANCE_FRACTION * min(
            prior_b.sensor_width_px, prior_b.sensor_height_px
        )
        candidates_b = _filter_usable(
            detect_sources(frame_b).sources, image_shape=frame_b.shape
        )
        nearby = [
            source
            for source in candidates_b
            if _distance((source.x, source.y), predicted_point) <= match_tolerance_px
        ]
        if len(nearby) == 0:
            return _failure(
                RegistrationStatus.NO_VALID_REGISTRATION,
                predicted_point=predicted_point,
                match_tolerance_px=match_tolerance_px,
                candidate_count_b=len(candidates_b),
            )
        if len(nearby) > 1:
            return _failure(
                RegistrationStatus.AMBIGUOUS_MATCH,
                predicted_point=predicted_point,
                match_tolerance_px=match_tolerance_px,
                candidates_b=[
                    (source.x, source.y, _distance((source.x, source.y), predicted_point))
                    for source in nearby
                ],
            )
        matched = nearby[0]
        matched_point = (matched.x, matched.y)

        # Refine the nominal (centered) polygon so the reference point
        # maps EXACTLY onto the real, measured matched point -- the
        # actual registration value-add over the nominal guess.
        local_x = reference_point[0] - prior_a.sensor_width_px / 2.0
        local_y = reference_point[1] - prior_a.sensor_height_px / 2.0
        theta = math.radians(assumed_rotation_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        rotated_x = scale * (cos_t * local_x - sin_t * local_y)
        rotated_y = scale * (sin_t * local_x + cos_t * local_y)
        solved_center_b = (matched_point[0] - rotated_x, matched_point[1] - rotated_y)

        final_polygon = rect_polygon(
            prior_a.sensor_width_px * scale,
            prior_a.sensor_height_px * scale,
            center=solved_center_b,
            rotation_deg=assumed_rotation_deg,
        )
        frame_b_rect = rect_polygon(
            prior_b.sensor_width_px, prior_b.sensor_height_px, center=center_b
        )
        overlap = overlap_polygon(final_polygon, frame_b_rect)
        status = RegistrationStatus.OK_OVERLAP if overlap else RegistrationStatus.OK_NO_OVERLAP

        return CrossCameraRegistrationResult(
            method=RegistrationMethod.ARTIFICIAL_STAR,
            status=status,
            rotation_deg=assumed_rotation_deg,
            scale=scale,
            polygon_a_in_b=final_polygon,
            overlap_polygon=overlap,
            confidence=1.0,
            diagnostics={
                "reference_point": reference_point,
                "predicted_point": predicted_point,
                "matched_point": matched_point,
                "match_tolerance_px": match_tolerance_px,
                "assumed_rotation_deg": assumed_rotation_deg,
                "rotation_measured": False,
            },
        )
