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

The star is located with `find_star_candidates` (one candidate per blob), not the generic
detector: field bundle 10e89885, a large defocused disc was split into 7953 "stars" after
minutes of search. A clipped or donut-shaped blob is a valid artificial star; its position
is the middle of the lit disc.

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

from astrotool_core.focus.star_candidates import (
    StarCandidate,
    find_star_candidates,
    frame_statistics,
    remove_hot_pixels,
)
from astrotool_core.registration.alignment import transform_point_a_to_b
from astrotool_core.registration.geometry import overlap_polygon, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior, scale_ratio
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)

#: A candidate this close to any frame edge is rejected -- an
#: implementation decision (the issue gives no exact number), mirroring
#: `collimation_measurement.detect_clipping`'s own margin-based
#: approach for a different (ring-fit) use case.
_EDGE_MARGIN_PX = 10.0

#: A dip between two bright parts shallower than this share of their brightness does not
#: separate them: rings, spider shadows and speckle stay one disc, while two separate stars
#: (background between them) stay two.
_BLOB_PROMINENCE_FRACTION = 0.9
#: The disc is every pixel above this share of the way from background to its bright level,
#: within this many blob radii of the blob.
_DISC_LEVEL_FRACTION = 0.25
_DISC_WINDOW_RADII = 1.2

#: Search radius (in B/larger-FOV pixels) around the predicted position
#: within which a Guide candidate is considered a plausible match -- a
#: fraction of Guide's own smaller sensor dimension. An implementation
#: decision, generous enough to tolerate real-world Main/Guide
#: misalignment (the whole point of this mode is to help fix that),
#: tight enough to pick the right one of several sources. A single source in B is the
#: artificial star wherever it is (field bundle 10e89885: 0.6 deg from the prediction).
_MATCH_TOLERANCE_FRACTION = 0.35


def _failure(status: RegistrationStatus, **diagnostics: object) -> CrossCameraRegistrationResult:
    return CrossCameraRegistrationResult(
        method=RegistrationMethod.ARTIFICIAL_STAR, status=status, diagnostics=diagnostics
    )


def _disc_center(
    image: np.ndarray, background: float, candidate: StarCandidate
) -> tuple[float, float]:
    """The middle of the lit area around `candidate` (unweighted, so an uneven brightness
    does not pull it towards the bright side)."""
    reach = int(candidate.radius_px * _DISC_WINDOW_RADII) + 2
    x0 = max(0, int(candidate.x) - reach)
    y0 = max(0, int(candidate.y) - reach)
    window = image[y0 : int(candidate.y) + reach + 1, x0 : int(candidate.x) + reach + 1]
    bright = float(np.percentile(window, 99.5))
    ys, xs = np.nonzero(window > background + _DISC_LEVEL_FRACTION * (bright - background))
    if xs.size == 0:
        return (candidate.x, candidate.y)
    left, right = np.percentile(xs, (0.5, 99.5))
    top, bottom = np.percentile(ys, (0.5, 99.5))
    return (x0 + float(left + right) / 2.0, y0 + float(top + bottom) / 2.0)


def _locate_stars(frame: np.ndarray) -> list[tuple[float, float]]:
    """The centre of every star blob that stays clear of the frame edge."""
    pixels = np.asarray(frame, dtype=np.float32)
    candidates = find_star_candidates(pixels, prominence_fraction=_BLOB_PROMINENCE_FRACTION)
    if not candidates:
        return []
    stats = frame_statistics(pixels)
    cleaned = remove_hot_pixels(pixels, stats.pixel_background, stats.pixel_noise)
    height, width = pixels.shape
    centers = [
        _disc_center(cleaned, stats.pixel_background, candidate)
        for candidate in candidates
        if not candidate.touches_edge
    ]
    stars: list[tuple[float, float]] = []
    for x, y in centers:
        if not (
            _EDGE_MARGIN_PX <= x <= width - _EDGE_MARGIN_PX
            and _EDGE_MARGIN_PX <= y <= height - _EDGE_MARGIN_PX
        ):
            continue
        # A core and its diffraction ring can be two blobs with the same disc.
        if all(_distance((x, y), star) > _EDGE_MARGIN_PX for star in stars):
            stars.append((x, y))
    return stars


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

        candidates_a = _locate_stars(frame_a)
        if len(candidates_a) != 1:
            return _failure(
                RegistrationStatus.INSUFFICIENT_STARS,
                candidate_count_a=len(candidates_a),
                candidates_a=candidates_a,
            )
        reference_point = candidates_a[0]

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
        candidates_b = _locate_stars(frame_b)
        nearby = [
            point
            for point in candidates_b
            if len(candidates_b) == 1 or _distance(point, predicted_point) <= match_tolerance_px
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
                    (*point, _distance(point, predicted_point)) for point in nearby
                ],
            )
        matched_point = nearby[0]

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
