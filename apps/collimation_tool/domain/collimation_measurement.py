"""Rough (donut-based) SCT collimation measurement.

Ported from smart_telescope's `domain/collimation/models.py` (geometry
primitives), `domain/collimation/processing/{stretch,geometry_fits,
donut_detection}.py`. All pure NumPy + dataclasses in the source, so this
is a near-verbatim port — the only mechanical change is taking an
astrotool_core `AnalysisPlane` (`.mono`/`.width`/`.height`) wherever the
source took its own `ProcessedFrame`.

Scope note (Stage 5): only the rough, donut-based collimation pathway is
ported. The Tri-Bahtinov mask fine-collimation pathway (spike detection/
decomposition/smoothing, contradiction detection, mask-sector mapping) is
deferred to a later stage — see docs/porting-notes.md.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np
from astrotool_core.frames.analysis_plane import AnalysisPlane

_log = logging.getLogger(__name__)


# ── Geometry primitives ──────────────────────────────────────────────────


class Point2D(NamedTuple):
    """Pixel coordinate (x = column, y = row)."""

    x: float
    y: float

    def distance_to(self, other: Point2D) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def __sub__(self, other: object) -> Point2D:
        if not isinstance(other, Point2D):
            return NotImplemented
        return Point2D(self.x - other.x, self.y - other.y)


@dataclass(frozen=True)
class ReferenceCenterCalibration:
    """Optical-axis reference calibration.

    source='frame_center' (default): reference == frame center; offsets
    are ignored. source='calibrated': reference = frame_center +
    (offset_x_px, offset_y_px) — the measured displacement from the
    geometric frame center to the true optical axis center.
    """

    offset_x_px: float = 0.0
    offset_y_px: float = 0.0
    source: str = "frame_center"  # "frame_center" | "calibrated"

    def compute(self, frame_width: int, frame_height: int) -> Point2D:
        cx = frame_width / 2.0
        cy = frame_height / 2.0
        if self.source == "calibrated":
            cx += self.offset_x_px
            cy += self.offset_y_px
        return Point2D(cx, cy)

    @property
    def is_calibrated(self) -> bool:
        return self.source == "calibrated"

    @property
    def has_offset(self) -> bool:
        return self.offset_x_px != 0.0 or self.offset_y_px != 0.0


@dataclass(frozen=True)
class CircleEllipseFit:
    """Fitted circle or ellipse in image pixel coordinates."""

    center_x: float
    center_y: float
    radius_x: float  # semi-major axis (pixels); for circles == radius_y
    radius_y: float  # semi-minor axis (pixels)
    angle_deg: float = 0.0  # major-axis rotation, 0 = horizontal
    confidence: float = 0.0  # 0-1; fit quality

    @property
    def center(self) -> Point2D:
        return Point2D(self.center_x, self.center_y)

    @property
    def is_circle(self) -> bool:
        """True when ellipse is close enough to circular (< 5% difference)."""
        if self.radius_x <= 0 or self.radius_y <= 0:
            return True
        ratio = min(self.radius_x, self.radius_y) / max(self.radius_x, self.radius_y)
        return ratio >= 0.95

    @property
    def eccentricity(self) -> float:
        a = max(self.radius_x, self.radius_y)
        b = min(self.radius_x, self.radius_y)
        if a <= 0:
            return 0.0
        return math.sqrt(1.0 - (b / a) ** 2)

    @property
    def mean_radius(self) -> float:
        return (self.radius_x + self.radius_y) / 2.0


# ── Display/background utilities (ported from processing/stretch.py) ────


def estimate_background(data: np.ndarray) -> tuple[float, float]:
    """Return (background_median, background_sigma) via iterative sigma-clipping."""
    flat = data.ravel().astype(np.float64)
    for _ in range(5):
        median = float(np.median(flat))
        sigma = float(np.std(flat))
        if sigma == 0.0:
            return median, 1.0
        clipped = flat[flat < median + 3.0 * sigma]
        if len(clipped) < 10 or len(clipped) == len(flat):
            flat = clipped if len(clipped) >= 10 else flat
            break
        flat = clipped

    bg = float(np.median(flat))
    sigma = float(np.std(flat)) if len(flat) > 1 else 1.0
    return bg, max(sigma, 1.0)


def auto_stretch(
    data: np.ndarray,
    low_percentile: float = 0.5,
    high_percentile: float = 99.9,
) -> np.ndarray:
    """Return a uint8 contrast-stretched copy for display."""
    lo = float(np.percentile(data, low_percentile))
    hi = float(np.percentile(data, high_percentile))
    if hi <= lo:
        hi = lo + 1.0
    stretched = np.clip((data.astype(np.float32) - lo) / (hi - lo), 0.0, 1.0)
    result: np.ndarray = (stretched * 255.0).astype(np.uint8)
    return result


def saturation_fraction(data: np.ndarray, bit_depth: int) -> float:
    """Return fraction [0, 1] of pixels at or above 99% of full-well capacity."""
    threshold = float(2**bit_depth - 1) * 0.99
    return float(np.sum(data >= threshold)) / max(1, data.size)


def peak_location(data: np.ndarray) -> tuple[float, float, float]:
    """Return (col, row, value) of the brightest pixel."""
    idx = int(np.argmax(data))
    row, col = divmod(idx, data.shape[1])
    return float(col), float(row), float(data.ravel()[idx])


# ── Circle/ellipse fitting (ported from processing/geometry_fits.py) ────


def fit_circle(points: np.ndarray) -> CircleEllipseFit:
    """Algebraic circle fit (Kasa / linear least-squares). Needs >= 3 points."""
    if len(points) < 3:
        return _degenerate_circle()

    x = points[:, 0].astype(np.float64)
    y = points[:, 1].astype(np.float64)
    a_matrix = np.column_stack([2.0 * x, 2.0 * y, np.ones(len(x))])
    b_vec = x**2 + y**2

    try:
        result, _, rank, _ = np.linalg.lstsq(a_matrix, b_vec, rcond=None)
    except np.linalg.LinAlgError:
        return _degenerate_circle()
    if rank < 3:
        return _degenerate_circle()

    cx, cy = float(result[0]), float(result[1])
    r_sq = float(result[2]) + cx**2 + cy**2
    if r_sq <= 0.0:
        return _degenerate_circle()
    r = float(np.sqrt(r_sq))

    dist = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    rms = float(np.sqrt(np.mean((dist - r) ** 2)))
    confidence = max(0.0, min(1.0, 1.0 - rms / max(r, 1.0)))

    return CircleEllipseFit(
        center_x=cx, center_y=cy, radius_x=r, radius_y=r, angle_deg=0.0, confidence=confidence
    )


def fit_ellipse(points: np.ndarray) -> CircleEllipseFit:
    """Direct algebraic ellipse fit (Bookstein constraint). Falls back to
    fit_circle when N < 5, the fit is degenerate, or the conic isn't an
    ellipse."""
    if len(points) < 5:
        return fit_circle(points)

    x = points[:, 0].astype(np.float64)
    y = points[:, 1].astype(np.float64)

    a_matrix = np.column_stack([x**2 - y**2, x * y, x, y, np.ones(len(x))])
    b_vec = -(y**2)

    try:
        result, _, rank, _ = np.linalg.lstsq(a_matrix, b_vec, rcond=None)
    except np.linalg.LinAlgError:
        return fit_circle(points)
    if rank < 5:
        return fit_circle(points)

    a_coef = float(result[0])
    b_coef = float(result[1])
    c_coef = 1.0 - a_coef  # Bookstein: a + c = 1
    d_coef = float(result[2])
    e_coef = float(result[3])
    f_coef = float(result[4])

    discriminant = b_coef**2 - 4.0 * a_coef * c_coef
    if discriminant >= 0.0:
        return fit_circle(points)

    fit = _conic_to_ellipse(a_coef, b_coef, c_coef, d_coef, e_coef, f_coef)
    if fit is None:
        return fit_circle(points)

    alg_residuals = (
        a_coef * x**2 + b_coef * x * y + c_coef * y**2 + d_coef * x + e_coef * y + f_coef
    )
    rms_alg = float(np.sqrt(np.mean(alg_residuals**2)))
    scale = max(fit.radius_x, fit.radius_y, 1.0)
    confidence = max(0.0, min(1.0, 1.0 - rms_alg / scale))

    return CircleEllipseFit(
        center_x=fit.center_x,
        center_y=fit.center_y,
        radius_x=fit.radius_x,
        radius_y=fit.radius_y,
        angle_deg=fit.angle_deg,
        confidence=confidence,
    )


def extract_edge_points(mask: np.ndarray) -> np.ndarray:
    """Extract (x, y) edge coordinates from a boolean mask (4-connected boundary)."""
    if not np.any(mask):
        return np.zeros((0, 2), dtype=np.float64)

    interior = np.zeros_like(mask)
    interior[1:-1, 1:-1] = (
        mask[1:-1, 1:-1] & mask[:-2, 1:-1] & mask[2:, 1:-1] & mask[1:-1, :-2] & mask[1:-1, 2:]
    )
    edge = mask & ~interior

    rows, cols = np.where(edge)
    return np.column_stack([cols.astype(np.float64), rows.astype(np.float64)])


def detect_clipping(
    fit: CircleEllipseFit,
    frame_width: int,
    frame_height: int,
    margin_px: float = 2.0,
) -> bool:
    """True when the fitted circle/ellipse is clipped by the frame edge."""
    r_max = max(fit.radius_x, fit.radius_y)
    return (
        fit.center_x - r_max < margin_px
        or fit.center_x + r_max > frame_width - margin_px
        or fit.center_y - r_max < margin_px
        or fit.center_y + r_max > frame_height - margin_px
    )


def compare_circle_centers(fit1: CircleEllipseFit, fit2: CircleEllipseFit) -> float:
    """Euclidean distance between the two fitted centers (pixels)."""
    dx = fit1.center_x - fit2.center_x
    dy = fit1.center_y - fit2.center_y
    return float(np.sqrt(dx**2 + dy**2))


def _degenerate_circle() -> CircleEllipseFit:
    return CircleEllipseFit(center_x=0.0, center_y=0.0, radius_x=0.0, radius_y=0.0)


def _conic_to_ellipse(
    a: float, b: float, c: float, d: float, e: float, f: float
) -> CircleEllipseFit | None:
    m_matrix = np.array([[2.0 * a, b], [b, 2.0 * c]])
    rhs = np.array([-d, -e])
    try:
        center = np.linalg.solve(m_matrix, rhs)
    except np.linalg.LinAlgError:
        return None

    cx, cy = float(center[0]), float(center[1])
    f_c = a * cx**2 + b * cx * cy + c * cy**2 + d * cx + e * cy + f
    if f_c == 0.0:
        return None

    eig_vals = np.linalg.eigvalsh(np.array([[a, b / 2.0], [b / 2.0, c]]))
    lam1, lam2 = float(eig_vals[0]), float(eig_vals[1])
    if lam1 <= 0.0 or lam2 <= 0.0:
        return None

    r1 = float(np.sqrt(abs(f_c) / lam1))
    r2 = float(np.sqrt(abs(f_c) / lam2))
    r_major, r_minor = max(r1, r2), min(r1, r2)

    _, eig_vecs = np.linalg.eigh(np.array([[a, b / 2.0], [b / 2.0, c]]))
    major_vec = eig_vecs[:, 0]
    angle_rad = float(np.arctan2(float(major_vec[1]), float(major_vec[0])))
    angle_deg = float(np.degrees(angle_rad)) % 180.0

    return CircleEllipseFit(
        center_x=cx, center_y=cy, radius_x=r_major, radius_y=r_minor, angle_deg=angle_deg
    )


# ── Donut (defocused-star) measurement (ported from processing/donut_detection.py) ─

_SIGNAL_SIGMA = 5.0
_MIN_RING_SIGMA = 3.0
_RING_FRACTION = 0.10
_MIN_EDGES = 6


@dataclass(frozen=True)
class DonutMeasurement:
    """Defocused-star (donut) measurement for rough SCT collimation.

    error_vector = inner_hole.center - outer_ring.center. A well-collimated
    scope has this vector close to (0, 0).
    """

    outer_ring: CircleEllipseFit
    inner_hole: CircleEllipseFit
    error_x_px: float
    error_y_px: float
    error_magnitude_px: float
    error_angle_deg: float
    confidence: float

    @property
    def error_vector(self) -> Point2D:
        return Point2D(self.error_x_px, self.error_y_px)

    @property
    def is_collimated(self) -> bool:
        """True when error < 2% of outer ring radius."""
        r = self.outer_ring.mean_radius
        return r > 0 and (self.error_magnitude_px / r) < 0.02


@dataclass(frozen=True)
class DonutAnalysisResult:
    """Outcome of donut analysis on one frame.

    measurement populated when reason == "ok"; reason otherwise one of
    "no_signal" | "no_ring_shape" | "inner_hole_unclear" | "clipped".
    """

    measurement: DonutMeasurement | None
    reason: str


class DonutAnalyzer:
    """Detect outer ring and inner shadow in a defocused star frame."""

    def __init__(self, signal_sigma: float = _SIGNAL_SIGMA, min_confidence: float = 0.15) -> None:
        self._sig_sigma = signal_sigma
        self._min_conf = min_confidence

    def analyze(self, plane: AnalysisPlane) -> DonutAnalysisResult:
        data = plane.mono
        bg, sigma = estimate_background(data)

        peak_val = float(np.max(data))
        if peak_val < bg + self._sig_sigma * sigma:
            return DonutAnalysisResult(measurement=None, reason="no_signal")

        ring_thresh = bg + max(_MIN_RING_SIGMA * sigma, (peak_val - bg) * _RING_FRACTION)
        bright = data > ring_thresh
        if not np.any(bright):
            return DonutAnalysisResult(measurement=None, reason="no_signal")

        bright_f = bright.astype(np.float64)
        total = bright_f.sum()
        rows_g = np.arange(plane.height, dtype=np.float64)[:, np.newaxis]
        cols_g = np.arange(plane.width, dtype=np.float64)[np.newaxis, :]
        cy = float((bright_f * rows_g).sum() / total)
        cx = float((bright_f * cols_g).sum() / total)

        dist_sq_grid = (rows_g - cy) ** 2 + (cols_g - cx) ** 2
        rms_sq = float((bright_f * dist_sq_grid).sum() / total)
        split_radius = float(np.sqrt(max(rms_sq, 1.0)))

        all_edges = extract_edge_points(bright)
        if len(all_edges) < _MIN_EDGES * 2:
            return DonutAnalysisResult(measurement=None, reason="no_ring_shape")

        edge_dist = np.sqrt((all_edges[:, 0] - cx) ** 2 + (all_edges[:, 1] - cy) ** 2)
        outer_edges = all_edges[edge_dist > split_radius]
        inner_edges = all_edges[edge_dist <= split_radius]
        if len(outer_edges) < _MIN_EDGES or len(inner_edges) < _MIN_EDGES:
            return DonutAnalysisResult(measurement=None, reason="no_ring_shape")

        outer_fit = fit_circle(outer_edges)
        inner_fit = fit_circle(inner_edges)

        _log.debug(
            "DonutAnalyzer outer=(%.1f,%.1f) r=%.1f conf=%.2f inner=(%.1f,%.1f) r=%.1f conf=%.2f",
            outer_fit.center_x,
            outer_fit.center_y,
            outer_fit.radius_x,
            outer_fit.confidence,
            inner_fit.center_x,
            inner_fit.center_y,
            inner_fit.radius_x,
            inner_fit.confidence,
        )

        if outer_fit.confidence < self._min_conf:
            return DonutAnalysisResult(measurement=None, reason="no_ring_shape")
        if inner_fit.confidence < self._min_conf:
            return DonutAnalysisResult(measurement=None, reason="inner_hole_unclear")

        if detect_clipping(outer_fit, plane.width, plane.height):
            return DonutAnalysisResult(measurement=None, reason="clipped")

        error_x = inner_fit.center_x - outer_fit.center_x
        error_y = inner_fit.center_y - outer_fit.center_y
        error_mag = math.hypot(error_x, error_y)
        error_ang = math.degrees(math.atan2(error_y, error_x))
        confidence = (outer_fit.confidence + inner_fit.confidence) / 2.0

        measurement = DonutMeasurement(
            outer_ring=outer_fit,
            inner_hole=inner_fit,
            error_x_px=error_x,
            error_y_px=error_y,
            error_magnitude_px=error_mag,
            error_angle_deg=error_ang,
            confidence=confidence,
        )
        return DonutAnalysisResult(measurement=measurement, reason="ok")
