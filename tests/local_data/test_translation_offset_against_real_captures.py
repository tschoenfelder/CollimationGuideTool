"""measure_translation_offset() tested against a real hardware capture
that exposed a confidently-wrong result -- local-only, never committed
(see local_test_data/terrestrial_saturated_guide_2026-09-08/README.md
for the full incident). Skipped entirely on any machine without the
dataset present.

Real report (diagnostic ef49ecb1-a052-44ea-b45e-01145a9f0c33): a real
terrestrial-mode guide-camera Test Move calibration measured BOTH
AXIS1's and AXIS2's real, independent pulses as an identical
`dx_px=0.0` at confidently-high scores (0.98, 0.71) -- geometrically
impossible for two roughly-orthogonal real mount axes, and only caught
downstream by `is_degenerate()`'s coincidental refusal to invert the
resulting matrix, not by this module noticing anything wrong itself.
Root cause: these frames are ~12-26% saturated (a large blown-out sky
region); see `_DEFAULT_MAX_SATURATED_FRACTION`'s own docstring in
`translation_offset.py` for the fix and the real numbers behind it. This
file pins the fixed behavior directly against the real frames that
exposed the bug.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astrotool_core.target.translation_offset import measure_translation_offset

_DATASET_DIR = (
    Path(__file__).resolve().parents[2]
    / "local_test_data"
    / "terrestrial_saturated_guide_2026-09-08"
    / "frames"
)

pytestmark = pytest.mark.skipif(
    not _DATASET_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_DATASET_DIR}",
)

_NOISY_MAIN_DATASET_DIR = (
    Path(__file__).resolve().parents[2]
    / "local_test_data"
    / "terrestrial_noisy_main_2026-09-08"
    / "frames"
)

_FEATURELESS_MAIN_DATASET_DIR = (
    Path(__file__).resolve().parents[2]
    / "local_test_data"
    / "terrestrial_featureless_main_2026-09-08"
    / "frames"
)


def _load(name: str) -> np.ndarray:
    data = fits.getdata(_DATASET_DIR / f"{name}.fits")
    return np.asarray(data, dtype=np.float32)


def _load_noisy_main(name: str) -> np.ndarray:
    data = fits.getdata(_NOISY_MAIN_DATASET_DIR / f"{name}.fits")
    return np.asarray(data, dtype=np.float32)


def _load_featureless_main(name: str) -> np.ndarray:
    data = fits.getdata(_FEATURELESS_MAIN_DATASET_DIR / f"{name}.fits")
    return np.asarray(data, dtype=np.float32)


@pytest.mark.parametrize("axis_name", ["axis1", "axis2"])
def test_saturated_guide_frames_now_report_no_usable_match(axis_name: str) -> None:
    """Before this fix: both axes wrongly reported dx_px=0.0 at scores
    0.98/0.71 (both above _DEFAULT_MIN_SCORE). After: the new saturation
    guard refuses both pairs outright, exactly like detect_sources()
    finding no star -- the correct behavior for a frame this saturated,
    rather than a confidently-wrong displacement caught only by luck
    downstream in is_degenerate()."""
    before = _load(f"guide_{axis_name}_before")
    after = _load(f"guide_{axis_name}_after")

    assert measure_translation_offset(before, after) is None


@pytest.mark.parametrize("axis_name", ["axis1", "axis2"])
def test_mains_own_unsaturated_frames_from_the_same_run_still_match(axis_name: str) -> None:
    """Cross-check: Main's own frames from the SAME real run are not
    saturated to nearly this degree and its calibration legitimately
    succeeded -- the new guard must not regress that."""
    before = _load(f"main_{axis_name}_before")
    after = _load(f"main_{axis_name}_after")

    assert measure_translation_offset(before, after) is not None


@pytest.mark.skipif(
    not _NOISY_MAIN_DATASET_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_NOISY_MAIN_DATASET_DIR}",
)
@pytest.mark.parametrize("axis_name", ["axis1", "axis2"])
def test_noisy_main_frames_now_recover_a_real_match_via_the_fallback(axis_name: str) -> None:
    """Real incident 93ba361f-18c6-46f6-9a53-fd05be821b01 (see
    local_test_data/terrestrial_noisy_main_2026-09-08/README.md for the
    full writeup): before this fix, both of Main's real axis pairs scored
    below _DEFAULT_MIN_SCORE at full resolution (0.098/0.093) despite
    real, human-visible structure -- independent per-pixel sensor noise
    swamped the whole-frame energy normalization. After: the downsampled
    fallback recovers a real match for both, using the actual frames
    that exposed the bug, not just a synthetic reconstruction."""
    before = _load_noisy_main(f"{axis_name}_before_left")
    after = _load_noisy_main(f"{axis_name}_after_left")

    assert measure_translation_offset(before, after) is not None


@pytest.mark.skipif(
    not _FEATURELESS_MAIN_DATASET_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_FEATURELESS_MAIN_DATASET_DIR}",
)
@pytest.mark.parametrize("axis_name", ["axis1", "axis2"])
def test_featureless_main_frames_report_no_usable_match_not_a_spurious_zero(
    axis_name: str,
) -> None:
    """Real incident 6cb859d2-7a94-4e44-8aff-585f0bf2466b (see
    local_test_data/terrestrial_featureless_main_2026-09-08/README.md for
    the full writeup): the very next real pair to hit the x8 fallback
    after it shipped -- a genuinely featureless real Main-camera pair
    (pure sensor grain, no discernible structure at all) whose full-
    resolution score (0.036/0.037) was already correctly below
    _DEFAULT_MIN_SCORE, but whose x8 fallback alone produced a confident,
    spurious (dx=0, dy=0) match (score 0.62-0.63) -- caught only by
    is_degenerate() downstream, reported as a misleading
    "confidently-measured zero, may be a mount/cable issue" rather than
    the real "nothing to measure here" cause. After this fix (the
    fallback's own plateau-validation against a second, coarser attempt):
    both axis pairs correctly report no usable match, using the actual
    frames that exposed the bug."""
    before = _load_featureless_main(f"{axis_name}_before_left")
    after = _load_featureless_main(f"{axis_name}_after_left")

    assert measure_translation_offset(before, after) is None
