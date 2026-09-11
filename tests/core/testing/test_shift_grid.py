"""Sanity checks on the shared known-shift grid (issues #28/#32) -- catches
an editing mistake (a duplicate name, a magnitude outside the documented
0-1000px range, a missing direction/sign class) rather than exercising any
algorithm; the grid itself is declarative data, not logic."""

from __future__ import annotations

from astrotool_core.testing.shift_grid import CI_SHIFT_GRID_SUBSET, KNOWN_SHIFT_GRID


def test_every_case_name_is_unique() -> None:
    names = [case.name for case in KNOWN_SHIFT_GRID]
    assert len(names) == len(set(names))


def test_every_magnitude_is_within_the_documented_0_to_1000px_range() -> None:
    for case in KNOWN_SHIFT_GRID:
        assert 0 <= case.magnitude_px <= 1000, case.name


def test_zero_shift_is_present() -> None:
    assert any(case.dx == 0 and case.dy == 0 for case in KNOWN_SHIFT_GRID)


def test_every_bucket_is_represented() -> None:
    buckets = {case.bucket for case in KNOWN_SHIFT_GRID}
    assert buckets == {"zero", "small", "medium", "large"}


def test_every_direction_class_is_represented() -> None:
    directions = {case.direction for case in KNOWN_SHIFT_GRID}
    assert directions == {"zero", "x_only", "y_only", "diagonal"}


def test_all_four_diagonal_sign_combinations_are_covered() -> None:
    """Issue #32's explicit (+X,+Y) (+X,-Y) (-X,+Y) (-X,-Y) list."""
    signs = {
        (1 if case.dx > 0 else -1, 1 if case.dy > 0 else -1)
        for case in KNOWN_SHIFT_GRID
        if case.direction == "diagonal"
    }
    assert signs == {(1, 1), (1, -1), (-1, 1), (-1, -1)}


def test_both_signs_are_covered_for_x_only_and_y_only() -> None:
    x_only_signs = {case.dx > 0 for case in KNOWN_SHIFT_GRID if case.direction == "x_only"}
    y_only_signs = {case.dy > 0 for case in KNOWN_SHIFT_GRID if case.direction == "y_only"}
    assert x_only_signs == {True, False}
    assert y_only_signs == {True, False}


def test_a_magnitude_near_ten_pixels_is_covered() -> None:
    """Issue #32's own "~10 to 1000 px" wording -- not just the extremes."""
    assert any(case.magnitude_px <= 15 and case.bucket != "zero" for case in KNOWN_SHIFT_GRID)


def test_ci_subset_is_a_subset_of_the_full_grid_and_covers_every_bucket_and_direction() -> None:
    full_names = {case.name for case in KNOWN_SHIFT_GRID}
    assert {case.name for case in CI_SHIFT_GRID_SUBSET} <= full_names
    assert len(CI_SHIFT_GRID_SUBSET) < len(KNOWN_SHIFT_GRID)
    assert {case.bucket for case in CI_SHIFT_GRID_SUBSET} == {"zero", "small", "medium", "large"}
    assert {case.direction for case in CI_SHIFT_GRID_SUBSET} == {
        "zero",
        "x_only",
        "y_only",
        "diagonal",
    }
