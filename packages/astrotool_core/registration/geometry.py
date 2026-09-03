"""Pure 2D polygon/rotation geometry shared by both registrars — issue
#29's "Shared helpers are appropriate for: optical/FOV calculation;
transform geometry; polygons/overlap; coordinate conventions;
diagnostics. The evidence-extraction/matching engines remain separate."

Every polygon here is a plain tuple of `(x, y)` points in image-space
convention (x right, y down, matching `AxisResponse.angle_degrees` and
`fov_registration`'s own established convention elsewhere in this
project) — never a `shapely` (or similar) object, so this module has no
extra dependency beyond plain arithmetic. A rotated rectangle (the only
shape either registrar ever produces — a camera sensor's own footprint)
is always convex, which is what lets `clip_polygon` use the simple
Sutherland–Hodgman algorithm rather than a general (concave-capable)
polygon clipper.

Deliberately knows nothing about cameras, mount motion, ASTAP, or which
optical train is which — see this package's own docstring.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Point = tuple[float, float]
Polygon = tuple[Point, ...]


def rect_polygon(
    width_px: float, height_px: float, *, center: Point = (0.0, 0.0), rotation_deg: float = 0.0
) -> Polygon:
    """A `width_px` x `height_px` rectangle centered on `center`, rotated
    `rotation_deg` about its own center — the standard image-space
    forward rotation `[[cos,-sin],[sin,cos]]` (see
    `fov_registration._rotate_bilinear`'s own docstring for the same
    convention). Corner order: top-left, top-right, bottom-right,
    bottom-left of the *unrotated* rectangle."""
    half_w, half_h = width_px / 2.0, height_px / 2.0
    theta = math.radians(rotation_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    cx, cy = center
    corners_relative = ((-half_w, -half_h), (half_w, -half_h), (half_w, half_h), (-half_w, half_h))
    return tuple(
        (cx + cos_t * lx - sin_t * ly, cy + sin_t * lx + cos_t * ly) for lx, ly in corners_relative
    )


def translate_polygon(polygon: Polygon, dx: float, dy: float) -> Polygon:
    return tuple((x + dx, y + dy) for x, y in polygon)


def polygon_area(polygon: Polygon) -> float:
    """Shoelace formula. Always non-negative regardless of the polygon's
    own winding order (an empty or degenerate polygon returns 0.0)."""
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=True):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_centroid(polygon: Polygon) -> Point:
    """The vertex-average centroid — exact for the regular (rectangle)
    shapes this module deals in; not the area-weighted centroid a
    general concave polygon would need."""
    if not polygon:
        raise ValueError("polygon_centroid: polygon has no vertices")
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (sum(xs) / len(polygon), sum(ys) / len(polygon))


def _is_inside(point: Point, edge_start: Point, edge_end: Point) -> bool:
    """True if `point` is on the "inside" (left, for a counter-clockwise
    edge in this module's y-down convention -- see `clip_polygon`) of the
    directed edge `edge_start -> edge_end`."""
    ex, ey = edge_end[0] - edge_start[0], edge_end[1] - edge_start[1]
    px, py = point[0] - edge_start[0], point[1] - edge_start[1]
    return (ex * py - ey * px) <= 0.0


def _edge_intersection(p1: Point, p2: Point, edge_start: Point, edge_end: Point) -> Point:
    ex, ey = edge_end[0] - edge_start[0], edge_end[1] - edge_start[1]
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    denom = ex * dy - ey * dx
    if denom == 0.0:
        return p2  # parallel -- shouldn't happen when only called on a real crossing
    t = ((p1[0] - edge_start[0]) * dy - (p1[1] - edge_start[1]) * dx) / denom
    return (edge_start[0] + t * ex, edge_start[1] + t * ey)


def clip_polygon(subject: Polygon, clip: Polygon) -> Polygon:
    """Sutherland–Hodgman clip of convex `subject` against convex `clip`
    -- both must be wound consistently (this module's own `rect_polygon`
    always produces one consistent winding; a polygon built from external
    corner points, e.g. WCS-projected ones, may need `_ensure_ccw` first
    -- see `star_field_registrar`). Returns an empty tuple if the two
    polygons don't overlap at all -- see `RegistrationStatus.OK_NO_OVERLAP`
    /`NO_OVERLAP`, which callers build from an empty result here, not an
    exception."""
    if not subject or not clip:
        return ()
    output: list[Point] = list(subject)
    for edge_start, edge_end in zip(clip, clip[1:] + clip[:1], strict=True):
        if not output:
            return ()
        input_list = output
        output = []
        for i in range(len(input_list)):
            current = input_list[i]
            previous = input_list[i - 1]
            current_inside = _is_inside(current, edge_start, edge_end)
            previous_inside = _is_inside(previous, edge_start, edge_end)
            if current_inside:
                if not previous_inside:
                    output.append(_edge_intersection(previous, current, edge_start, edge_end))
                output.append(current)
            elif previous_inside:
                output.append(_edge_intersection(previous, current, edge_start, edge_end))
    return tuple(output)


def ensure_ccw(polygon: Polygon) -> Polygon:
    """Reorders `polygon`'s vertices to counter-clockwise winding in this
    module's y-down image-space convention (the winding `clip_polygon`'s
    `_is_inside` assumes) if it isn't already -- needed for polygons built
    from externally-derived points (e.g. `star_field_registrar`'s
    WCS-projected sky corners), whose winding isn't controlled by this
    module the way `rect_polygon`'s own output always is."""
    if polygon_area(polygon) == 0.0 or len(polygon) < 3:
        return polygon
    # `_is_inside` expects the winding whose shoelace sign comes out
    # *negative* in this module's y-down image space -- confirmed
    # empirically by `clip_polygon` working correctly on two
    # `ensure_ccw`-passed `rect_polygon` outputs (see `overlap_polygon`,
    # always called through this function, never used raw).
    signed = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(polygon, polygon[1:] + polygon[:1], strict=True)
    )
    return polygon if signed < 0.0 else tuple(reversed(polygon))


def overlap_polygon(a: Polygon, b: Polygon) -> Polygon:
    """The overlap region of two convex polygons (any winding), or an
    empty tuple if they don't overlap at all."""
    return clip_polygon(ensure_ccw(a), ensure_ccw(b))


def overlap_area(a: Polygon, b: Polygon) -> float:
    return polygon_area(overlap_polygon(a, b))


def fully_contains(outer: Polygon, inner: Polygon, *, tolerance: float = 1e-6) -> bool:
    """True if `inner` lies entirely within `outer` -- checked as "the
    overlap area equals inner's own area" (within `tolerance`, relative
    to inner's area) rather than a per-vertex inside check, so it's exact
    even when `inner` isn't itself convex-clipped-friendly in edge cases
    (shared edges, near-tangent corners)."""
    inner_area = polygon_area(inner)
    if inner_area == 0.0:
        return False
    overlap = overlap_area(outer, inner)
    return abs(overlap - inner_area) <= tolerance * inner_area


def polygon_bounds(polygon: Polygon) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y)."""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def polygon_from_points(points: Sequence[Point]) -> Polygon:
    return tuple(points)
