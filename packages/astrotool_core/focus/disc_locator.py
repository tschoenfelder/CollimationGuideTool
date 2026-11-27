"""Where the lit disc of a single bright target is -- focused, defocused, ringed or clipped.

Built on `find_star_candidates` (one candidate per blob, fast on large discs), unlike the
generic `detect_sources`, which splits a large speckled disc into thousands of "stars" after
minutes of search (field bundle 10e89885).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from astrotool_core.focus.star_candidates import (
    StarCandidate,
    find_star_candidates,
    frame_statistics,
    remove_hot_pixels,
)

#: A dip between two bright parts shallower than this share of their brightness does not
#: separate them: rings, spider shadows and speckle stay one disc, while two separate stars
#: (background between them) stay two.
_BLOB_PROMINENCE_FRACTION = 0.9
#: The disc is every pixel above this share of the way from background to its bright level,
#: within this many blob radii of the blob.
_DISC_LEVEL_FRACTION = 0.25
_DISC_WINDOW_RADII = 1.2
#: Two blobs whose disc centres are this close (px) are one disc (a core and its ring).
_SAME_DISC_PX = 10.0
#: `locate_main_disc` bins a frame down to at most about this width.
_QUICK_WIDTH_PX = 960


@dataclass(frozen=True)
class Disc:
    """The centre of a lit disc in frame pixels. `radius_px`: half the larger extent of the
    lit area. `touches_edge`: the disc is cut by the frame border, so the centre is biased."""

    x: float
    y: float
    radius_px: float
    touches_edge: bool


def _disc_of(image: np.ndarray, background: float, candidate: StarCandidate) -> Disc:
    """The middle of the lit area around `candidate` (unweighted, so an uneven brightness
    does not pull it towards the bright side)."""
    reach = int(candidate.radius_px * _DISC_WINDOW_RADII) + 2
    x0 = max(0, int(candidate.x) - reach)
    y0 = max(0, int(candidate.y) - reach)
    window = image[y0 : int(candidate.y) + reach + 1, x0 : int(candidate.x) + reach + 1]
    bright = float(np.percentile(window, 99.5))
    ys, xs = np.nonzero(window > background + _DISC_LEVEL_FRACTION * (bright - background))
    if xs.size == 0:
        return Disc(candidate.x, candidate.y, candidate.radius_px, candidate.touches_edge)
    left, right = np.percentile(xs, (0.5, 99.5))
    top, bottom = np.percentile(ys, (0.5, 99.5))
    return Disc(
        x=x0 + float(left + right) / 2.0,
        y=y0 + float(top + bottom) / 2.0,
        radius_px=float(max(right - left, bottom - top)) / 2.0,
        touches_edge=candidate.touches_edge,
    )


def locate_discs(frame: np.ndarray) -> list[Disc]:
    """Every lit disc of `frame`, the largest first."""
    pixels = np.asarray(frame, dtype=np.float32)
    candidates = find_star_candidates(pixels, prominence_fraction=_BLOB_PROMINENCE_FRACTION)
    if not candidates:
        return []
    stats = frame_statistics(pixels)
    cleaned = remove_hot_pixels(pixels, stats.pixel_background, stats.pixel_noise)
    found = sorted(
        (_disc_of(cleaned, stats.pixel_background, candidate) for candidate in candidates),
        key=lambda disc: -disc.radius_px,
    )
    discs: list[Disc] = []
    for disc in found:
        if all(math.hypot(disc.x - o.x, disc.y - o.y) > _SAME_DISC_PX for o in discs):
            discs.append(disc)
    return discs


def locate_main_disc(frame: np.ndarray) -> Disc | None:
    """The largest disc of `frame`, found on a binned copy (quick enough for repeated use on
    a large sensor); None when nothing stands out."""
    pixels = np.asarray(frame, dtype=np.float32)
    b = max(1, pixels.shape[1] // _QUICK_WIDTH_PX)
    height, width = pixels.shape[0] // b, pixels.shape[1] // b
    if b > 1:
        pixels = pixels[: height * b, : width * b].reshape(height, b, width, b).mean(axis=(1, 3))
    discs = locate_discs(pixels)
    if not discs:
        return None
    disc = discs[0]
    offset = (b - 1) / 2.0
    return Disc(disc.x * b + offset, disc.y * b + offset, disc.radius_px * b, disc.touches_edge)
