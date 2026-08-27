"""Stage 8: wires every currently-unpopulated datasets/ leaf into a test.

Each placeholder leaf's README.md documents an "auto-skip convention": a
test reading that dataset must skip cleanly, not fail, when frames/ or
expected.json are absent. This file is that skeleton for the four leaves
that have no data yet. Once real frames + expected.json are captured for
one of them, replace its branch below with an actual replay/assert test
(see test_roi_tracker_replay.py / test_guide_lost_star_replay.py for the
pattern) — the deliberate ``pytest.fail`` below exists so that just
dropping data in without wiring a real test is caught, not silently
skipped forever.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from astrotool_core.testing.replay_dataset import discover_fits_paths

DATASETS_DIR = Path(__file__).resolve().parents[2] / "datasets"

_PLACEHOLDER_LEAVES = [
    "collimation/artificial_star",
    "collimation/color_bayer",
    "collimation/mono_centered",
    "guiding/steady_drift",
]


@pytest.mark.parametrize("leaf", _PLACEHOLDER_LEAVES)
def test_placeholder_dataset_skips_cleanly_when_unpopulated(leaf: str) -> None:
    dataset_dir = DATASETS_DIR / leaf
    has_frames = bool(discover_fits_paths(dataset_dir))
    has_expected = (dataset_dir / "expected.json").exists()

    if not has_frames and not has_expected:
        pytest.skip(f"{leaf}: placeholder dataset not yet populated (see its README.md)")

    pytest.fail(
        f"{leaf}: frames/expected.json are now present but no real golden-master "
        "test is wired for this dataset yet — replace this skip skeleton with one."
    )
