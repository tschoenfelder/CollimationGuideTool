"""Stage 2 short-exposure frame registration — issue #16: align
successive focused-star ROI frames (from Stage 1's own tracked ROI, issue
#15) to a common reference position so atmospheric image motion doesn't
smear the diffraction pattern before stacking (Stage 3, issue #17).

Deliberately does not rely on mount movement (issue's own text) — purely
a software re-sampling of each frame based on its own measured star
position. Operates on already-cropped, already-single-target ROI frames,
so `select_target` (brightest/`normal_star`, already rejects
`donut_like`) is sufficient here without issue #15's own
`resolve_identity` ambiguity handling -- that concern is Stage 1's own
full-frame/multi-candidate problem, not Stage 2's.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from astrotool_core.target.detector import detect_sources
from astrotool_core.target.point_source import PointSource
from astrotool_core.target.roi_selector import select_target


def shift_image(
    image: np.ndarray, dx: float, dy: float, *, fill_value: float | None = None
) -> np.ndarray:
    """Sub-pixel bilinear translation: the returned image's content at
    `(x, y)` is `image`'s own content at `(x - dx, y - dy)` -- i.e.
    shifting by `(dx, dy)` moves a star at `(sx, sy)` to `(sx + dx, sy +
    dy)`. `fill_value` defaults to `image`'s own median (not its mean --
    a small bright point source pulls the mean well above the true
    background, which would otherwise paint a spuriously elevated border
    strip wherever pixels shift in from outside the original frame,
    confirmed empirically: a synthetic single-star test frame's own mean
    sits well above its true background, comfortably enough to register
    as a second, spurious detected source at the border. The median
    stays at the true background level since a star only ever covers a
    tiny minority of pixels)."""
    if fill_value is None:
        fill_value = float(np.median(image))
    height, width = image.shape
    ys, xs = np.meshgrid(
        np.arange(height, dtype=np.float64), np.arange(width, dtype=np.float64), indexing="ij"
    )
    src_x = xs - dx
    src_y = ys - dy

    x0 = np.floor(src_x).astype(np.int64)
    y0 = np.floor(src_y).astype(np.int64)
    x1, y1 = x0 + 1, y0 + 1
    valid = (x0 >= 0) & (x1 <= width - 1) & (y0 >= 0) & (y1 <= height - 1)
    x0c, x1c = np.clip(x0, 0, width - 1), np.clip(x1, 0, width - 1)
    y0c, y1c = np.clip(y0, 0, height - 1), np.clip(y1, 0, height - 1)

    wx, wy = src_x - x0, src_y - y0
    image64 = image.astype(np.float64)
    top = image64[y0c, x0c] * (1 - wx) + image64[y0c, x1c] * wx
    bottom = image64[y1c, x0c] * (1 - wx) + image64[y1c, x1c] * wx
    interpolated = top * (1 - wy) + bottom * wy
    return np.where(valid, interpolated, fill_value)


@dataclass(frozen=True)
class FrameRegistrationOutcome:
    """`star`/`shift_dx`/`shift_dy` are `None` exactly when `usable` is
    `False` (AC 2.3: no selectable star, never added to the registered
    stack)."""

    usable: bool
    pixels: np.ndarray | None
    star: PointSource | None
    shift_dx: float | None
    shift_dy: float | None


@dataclass(frozen=True)
class FrameRegistrationResult:
    """`reference_position` is `None` only if every frame was unusable."""

    reference_position: tuple[float, float] | None
    outcomes: tuple[FrameRegistrationOutcome, ...]

    @property
    def usable_pixels(self) -> tuple[np.ndarray, ...]:
        return tuple(
            outcome.pixels
            for outcome in self.outcomes
            if outcome.usable and outcome.pixels is not None
        )


def register_frames(frames: Sequence[np.ndarray]) -> FrameRegistrationResult:
    """For each frame: `detect_sources` -> `select_target`. The FIRST
    usable frame's own measured position becomes `reference_position`
    (self-contained -- AC 2.1 only asks that frames align with each
    other, not with an externally imposed point); every usable frame
    (including that first one) is shifted via `shift_image` so its own
    star lands at `reference_position`."""
    stars: list[PointSource | None] = []
    for frame in frames:
        detection = detect_sources(frame)
        stars.append(select_target(detection))

    reference_position: tuple[float, float] | None = None
    for star in stars:
        if star is not None:
            reference_position = (star.x, star.y)
            break

    outcomes: list[FrameRegistrationOutcome] = []
    for frame, star in zip(frames, stars, strict=True):
        if star is None or reference_position is None:
            outcomes.append(FrameRegistrationOutcome(False, None, None, None, None))
            continue
        shift_dx = reference_position[0] - star.x
        shift_dy = reference_position[1] - star.y
        aligned = shift_image(frame, shift_dx, shift_dy)
        outcomes.append(FrameRegistrationOutcome(True, aligned, star, shift_dx, shift_dy))

    return FrameRegistrationResult(reference_position=reference_position, outcomes=tuple(outcomes))
