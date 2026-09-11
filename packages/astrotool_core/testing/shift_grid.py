"""Shared deterministic known-shift grid for the translation estimator's
real-frame corpus (issue #28) and its sparse/star-content coverage (issue
#32) -- both issues' own text explicitly allows this grid to be shared.

Every entry is an exact `(dx, dy)` pixel shift, applied via `numpy.roll`
(`np.roll(frame, shift=(dy, dx), axis=(0, 1))` -- note the (dy, dx) axis
order `numpy.roll` itself expects, matching every existing
`tests/core/target/test_translation_offset.py` fixture) to a real or
synthetic base frame. The expected displacement is always the applied
transform itself, never the estimator's own output (issue #28's explicit
requirement) -- this module only describes shifts, it never measures one.

Representative, not exhaustive: covers issue #28's small/medium/large range
(0 through 1000px) and issue #32's explicit 8-direction list ((+X,0) (-X,0)
(0,+Y) (0,-Y) (+X,+Y) (+X,-Y) (-X,+Y) (-X,-Y)), including magnitudes near
10px per #32's own "~10 to 1000 px" wording, not just the extremes.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Bucket boundaries, both issues' own "small/medium/large" language.
#: `magnitude` is `max(abs(dx), abs(dy))`.
_SMALL_MAX_PX = 64
_MEDIUM_MAX_PX = 512


@dataclass(frozen=True)
class ShiftCase:
    """One grid entry: an exact, documented `(dx, dy)` pixel shift."""

    name: str
    dx: int
    dy: int

    @property
    def magnitude_px(self) -> int:
        return max(abs(self.dx), abs(self.dy))

    @property
    def bucket(self) -> str:
        """`"zero"`, `"small"` (<64px), `"medium"` (64-512px), or `"large"`
        (>512px, up to the grid's own 1000px ceiling) -- matches the
        magnitude-vs-confidence bucketing `scripts/translation_estimator_
        benchmark.py`'s own report uses."""
        magnitude = self.magnitude_px
        if magnitude == 0:
            return "zero"
        if magnitude < _SMALL_MAX_PX:
            return "small"
        if magnitude <= _MEDIUM_MAX_PX:
            return "medium"
        return "large"

    @property
    def direction(self) -> str:
        """`"zero"`, `"x_only"`, `"y_only"`, or `"diagonal"`."""
        if self.dx == 0 and self.dy == 0:
            return "zero"
        if self.dy == 0:
            return "x_only"
        if self.dx == 0:
            return "y_only"
        return "diagonal"


#: The full grid: ~28 documented shifts spanning 0-1000px, both axes, all
#: four diagonal sign combinations, and both equal- and unequal-magnitude
#: diagonals. Not every integer 0-1000 (issue #28 explicitly says this
#: isn't required) -- a representative deterministic sample instead.
KNOWN_SHIFT_GRID: tuple[ShiftCase, ...] = (
    ShiftCase("zero", 0, 0),
    # X-only, both signs, small/medium/large.
    ShiftCase("x_small_pos", 10, 0),
    ShiftCase("x_small_neg", -10, 0),
    ShiftCase("x_medium_pos", 120, 0),
    ShiftCase("x_medium_neg", -120, 0),
    ShiftCase("x_large_pos", 640, 0),
    ShiftCase("x_large_neg", -640, 0),
    ShiftCase("x_max_pos", 1000, 0),
    ShiftCase("x_max_neg", -1000, 0),
    # Y-only, both signs, small/medium/large.
    ShiftCase("y_small_pos", 0, 10),
    ShiftCase("y_small_neg", 0, -10),
    ShiftCase("y_medium_pos", 0, 120),
    ShiftCase("y_medium_neg", 0, -120),
    ShiftCase("y_large_pos", 0, 640),
    ShiftCase("y_large_neg", 0, -640),
    ShiftCase("y_max_pos", 0, 1000),
    ShiftCase("y_max_neg", 0, -1000),
    # Diagonal, equal X/Y magnitude, all four sign combinations -- issue
    # #32's explicit (+X,+Y) (+X,-Y) (-X,+Y) (-X,-Y) list, at small and
    # medium magnitudes.
    ShiftCase("diag_equal_small_pp", 10, 10),
    ShiftCase("diag_equal_small_pn", 10, -10),
    ShiftCase("diag_equal_small_np", -10, 10),
    ShiftCase("diag_equal_small_nn", -10, -10),
    ShiftCase("diag_equal_medium_pp", 300, 300),
    ShiftCase("diag_equal_medium_pn", 300, -300),
    ShiftCase("diag_equal_medium_np", -300, 300),
    ShiftCase("diag_equal_medium_nn", -300, -300),
    # Diagonal, unequal X/Y magnitude, spanning bucket boundaries.
    ShiftCase("diag_unequal_pp", 120, 640),
    ShiftCase("diag_unequal_np", -120, 640),
    ShiftCase("diag_unequal_pn", 640, -120),
)

#: A small, curated subset for fast CI runs (`tests/core/target/
#: test_translation_offset.py`) -- one representative case per
#: bucket/direction combination actually present in the full grid, so CI
#: exercises every category without paying for all ~28 FFTs per test
#: parametrization. `scripts/translation_estimator_benchmark.py` and the
#: real-capture corpus use the full `KNOWN_SHIFT_GRID` instead.
CI_SHIFT_GRID_SUBSET: tuple[ShiftCase, ...] = tuple(
    case
    for case in KNOWN_SHIFT_GRID
    if case.name
    in {
        "zero",
        "x_small_pos",
        "x_large_neg",
        "y_small_neg",
        "y_large_pos",
        "diag_equal_small_pp",
        "diag_equal_medium_nn",
        "diag_unequal_pp",
    }
)
