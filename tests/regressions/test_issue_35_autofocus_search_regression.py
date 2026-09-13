"""Regression for issue #35 — real field failure, diagnostic UUID
73a007b6-6c9b-41e2-a3e5-66a21ec71ffd (see datasets/regressions/35/).

A terrestrial Auto Focus run on the real rig reported SUCCESS at focuser
position 15710, but the focuser ended up visibly out of focus. The
bundle's own recorded (position, value) curve never demonstrably turns
over before hitting the ±1000-step search boundary -- see
datasets/regressions/35/README.md's "Root cause" for the full trace.

This does not fit tests/regressions/_loader.py's generic frame-pair
`BOUNDARIES` driver (a decision-algorithm regression over a recorded
scalar curve, not an image-analysis regression -- no per-position
frames were preserved by the original capture). Bespoke instead: named
here so `test_every_regression_dataset_is_wired.py` finds it (it scans
this directory's own `test_*.py` modules for the dataset directory
name, "35", verbatim below), replaying the exact recorded sequence
through `BoundedFocusSearcher` via a scripted fake focuser/measurer.

Expected outcome (`BEST_AT_SEARCH_LIMIT`, not `SUCCESS`) was derived
independently in datasets/regressions/35/expected.json's own
`rationale` -- purely by inspecting the recorded values arithmetically,
never by running this implementation.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from astrotool_core.focus.fake_focuser import FakeFocuser
from collimation_tool.application.autofocus_search import (
    AutofocusStatus,
    BoundedFocusSearcher,
    FocusSample,
)

_DATASET_DIR = Path(__file__).resolve().parents[2] / "datasets" / "regressions" / "35"


def _load_recorded_samples() -> list[tuple[int, float]]:
    """The exact real (position, value) sequence from the bundle's own
    `incident.json` -- read from provenance, not hand-copied, so this
    test can't silently drift from the real evidence."""
    incident = json.loads((_DATASET_DIR / "provenance" / "incident.json").read_text())
    return [
        (sample["position"], sample["value"])
        for sample in incident["context"]["autofocus"]["samples"]
    ]


class _LargeRangeFocuser(FakeFocuser):
    """`FakeFocuser`'s own device max (5000) is too small to host the
    real bundle's actual position range (~14960-15960) -- only
    `get_max_position()` needs overriding, matching the real rig's own
    logged `max=100000`."""

    def get_max_position(self) -> int:
        return 100_000


def _replay_measurer(values: list[float]) -> Callable[[], FocusSample]:
    iterator = iter(values)

    def measure() -> FocusSample:
        return FocusSample(value=next(iterator), confidence=1.0)

    return measure


def test_terrestrial_curve_never_turns_over_before_search_boundary() -> None:
    samples = _load_recorded_samples()
    start_position = samples[0][0]
    assert start_position == 14960  # sanity check against the provenance file

    focuser = _LargeRangeFocuser()
    focuser.move_absolute(start_position)
    searcher = BoundedFocusSearcher(
        focuser,
        higher_is_better=True,  # terrestrial mode
        coarse_step=250,  # matches the real recorded move deltas exactly
        fine_step=25,
    )

    result = searcher.search(_replay_measurer([value for _, value in samples]))

    assert result.status is AutofocusStatus.BEST_AT_SEARCH_LIMIT
    assert result.start_position == 14960
    assert result.search_min == 13960
    assert result.search_max == 15960
