"""Shared tolerance-based golden-master comparison helper.

Stage 8: factored out of the per-key ``pytest.approx`` loops that
tests/integration/test_axis_calibration_replay.py and
test_guide_lost_star_replay.py were each writing by hand, per PLAN.md's
"Regression-protection scaffolding" section. test_roi_tracker_replay.py is
deliberately NOT converted to this helper — it compares a list of lock-state
names for exact equality, which isn't a numeric-tolerance comparison.
"""

from __future__ import annotations

from typing import Any

import pytest


def assert_matches_golden(
    actual: dict[str, Any], expected: dict[str, Any], *, tolerances: dict[str, float]
) -> None:
    """Assert every key in *tolerances* matches between *actual* and *expected*.

    Each key is compared with ``pytest.approx(expected[key], abs=tolerances[key])``.
    Both dicts must contain every key named in *tolerances*.
    """
    for key, tol in tolerances.items():
        message = (
            f"{key} drifted beyond tolerance {tol}: "
            f"actual={actual[key]!r} expected={expected[key]!r}"
        )
        assert actual[key] == pytest.approx(expected[key], abs=tol), message
