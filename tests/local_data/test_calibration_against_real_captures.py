"""Calibration algorithm tested against a real hardware capture --
local-only, never committed (the frames themselves live in
`local_test_data/`, which `.gitignore` excludes; see that entry's own
note). Skipped entirely on any machine without the dataset present.

Real report ("Failed again... for guide"): calibration kept failing
despite several already-shipped software fixes (degenerate-message
cross-camera wording, per-camera decoupling, two-stage frame-settle
wait). Rather than guess at another fix, captured a real 10-frame
dataset directly from the rig -- center, then AXIS1+/-, AXIS2+/- for
both Main and Guide (`scripts/`-adjacent one-off capture script, not
committed; see project memory for how it was run) -- and rendered every
frame to look at by eye before drawing conclusions.

Finding: AXIS1's *positive* pulse produced disproportionately little
real motion on both cameras -- Guide's own reading is a confident, exact
`(0, 0)` (score 0.997), while Main's is small but genuinely nonzero
(`dx=-18, dy=-15`, score 0.84) -- both far smaller than every other
pulse's real shift (Guide: 98-186px; Main: 800-1400px, both scaled up
from Guide's by this project's own established ~8.7x plate-scale-ratio
finding, not a measurement artifact). This matches the same "one pulse
produces far less real motion than the driver's full acceptance implies"
pattern already found in diagnostics 26869ea3 and 0270868c -- in both of
those *and* this run, it's specifically the axis's own *first* pulse in
that direction this session (AXIS1 here, AXIS2 in 26869ea3) that
underperforms, every subsequent pulse in either direction behaving
normally -- consistent with real mechanical backlash (the first
commanded steps in a freshly-reversed direction take up gear slack
instead of turning the axis) rather than a software bug in the
calibration/measurement path, which these tests confirm is already
behaving correctly against this exact real-world case.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astrotool_core.mount.axis_calibration import is_degenerate, response_from_positions
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.target.translation_offset import measure_translation_offset

_DATASET_DIR = (
    Path(__file__).resolve().parents[2]
    / "local_test_data"
    / "calibration_dataset_2026-09-02"
)

pytestmark = pytest.mark.skipif(
    not _DATASET_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_DATASET_DIR}",
)


def _load(name: str) -> np.ndarray:
    data = fits.getdata(_DATASET_DIR / f"{name}.fits")
    return np.asarray(data, dtype=np.float32)


@pytest.mark.parametrize(
    ("camera", "axis_name", "expect_zero"),
    [
        ("main", "axis1_positive", False),  # small but real: dx=-18, dy=-15 (backlash, not zero)
        ("main", "axis1_negative", False),
        ("main", "axis2_positive", False),
        ("main", "axis2_negative", False),
        ("guide", "axis1_positive", True),
        ("guide", "axis1_negative", False),
        ("guide", "axis2_positive", False),
        ("guide", "axis2_negative", False),
    ],
)
def test_measured_shift_matches_the_real_capture(
    camera: str, axis_name: str, expect_zero: bool
) -> None:
    """Pins this real dataset's own actual behavior -- a future
    algorithm change that silently stops matching this real-world case
    should fail here, not surface as another live "calibration failed"
    report."""
    before = _load(f"{camera}_center")
    after = _load(f"{camera}_{axis_name}")
    result = measure_translation_offset(before, after)
    assert result is not None  # every one of these scored well above threshold
    if expect_zero:
        assert result.dx_px == 0.0
        assert result.dy_px == 0.0
    else:
        assert result.dx_px != 0.0 or result.dy_px != 0.0


def test_axis1_positive_is_disproportionately_small_on_both_cameras() -> None:
    """The backlash signature: AXIS1's own first-tested-direction pulse
    measures far smaller than AXIS2's same-run pulse on *both* cameras --
    Guide's coarser plate scale rounds its own small real motion down to
    an exact `(0, 0)` (confirmed degenerate against AXIS2, below), while
    Main's finer plate scale still resolves it as small-but-nonzero, not
    zero. Pins the magnitude ratio itself rather than an exact value, so
    it survives small measurement noise while still catching a regression
    that makes AXIS1's first pulse behave like every other pulse."""
    for camera in ("main", "guide"):
        before = _load(f"{camera}_center")
        offset1 = measure_translation_offset(before, _load(f"{camera}_axis1_positive"))
        offset2 = measure_translation_offset(before, _load(f"{camera}_axis2_positive"))
        assert offset1 is not None and offset2 is not None
        mag1 = (offset1.dx_px**2 + offset1.dy_px**2) ** 0.5
        mag2 = (offset2.dx_px**2 + offset2.dy_px**2) ** 0.5
        assert mag1 < mag2 * 0.3, camera  # AXIS1+ is well under a third of AXIS2+'s magnitude


def test_guide_axis1_positive_is_degenerate_against_axis2_this_run() -> None:
    """Guide's exact-zero AXIS1 reading makes its own AXIS1-vs-AXIS2
    matrix degenerate (a zero vector is parallel to anything) --
    confirms is_degenerate() correctly flags this exact real scenario,
    which _finish_calibration_step's own per-camera exclusion (commit
    8f87897) depends on. Main's own AXIS1+ reading is small but *not*
    exactly zero (dx=-18, dy=-15), and is_degenerate() correctly does
    NOT flag Main's pair this run -- the determinant check is about
    near-parallel-ness, not smallness, and this particular small vector
    happens not to be near-parallel to Main's own AXIS2 vector."""
    guide_offset1 = measure_translation_offset(
        _load("guide_center"), _load("guide_axis1_positive")
    )
    guide_offset2 = measure_translation_offset(
        _load("guide_center"), _load("guide_axis2_positive")
    )
    assert guide_offset1 is not None and guide_offset2 is not None
    guide_axis1 = response_from_positions(
        MountAxis.AXIS1, AxisDirection.POSITIVE, 500, (0.0, 0.0),
        (guide_offset1.dx_px, guide_offset1.dy_px),
    )
    guide_axis2 = response_from_positions(
        MountAxis.AXIS2, AxisDirection.POSITIVE, 500, (0.0, 0.0),
        (guide_offset2.dx_px, guide_offset2.dy_px),
    )
    assert is_degenerate(guide_axis1, guide_axis2) is True

    main_offset1 = measure_translation_offset(
        _load("main_center"), _load("main_axis1_positive")
    )
    main_offset2 = measure_translation_offset(
        _load("main_center"), _load("main_axis2_positive")
    )
    assert main_offset1 is not None and main_offset2 is not None
    main_axis1 = response_from_positions(
        MountAxis.AXIS1, AxisDirection.POSITIVE, 500, (0.0, 0.0),
        (main_offset1.dx_px, main_offset1.dy_px),
    )
    main_axis2 = response_from_positions(
        MountAxis.AXIS2, AxisDirection.POSITIVE, 500, (0.0, 0.0),
        (main_offset2.dx_px, main_offset2.dy_px),
    )
    assert is_degenerate(main_axis1, main_axis2) is False


@pytest.mark.parametrize("shift_px", [10, 20])
def test_a_known_synthetic_shift_of_a_real_frame_is_recovered_exactly(shift_px: int) -> None:
    """Ground-truth check with a *known* answer, unlike the real
    axis-pulse pairs above (there, the true displacement is exactly
    what's being measured, not already known -- these can only be
    cross-checked against each other, e.g. via the plate-scale/degenerate
    tests). `tests/core/target/test_translation_offset.py` already pins
    "a known shift is recovered exactly" against synthetic per-pixel
    Gaussian noise; this is the same check against one real captured
    frame's own actual statistics (real sensor noise, real non-uniform
    scene texture) instead, using this dataset now that it exists.
    `np.roll` gives an exact circular shift, matching
    `measure_translation_offset()`'s own documented small-pulse
    assumption (same technique this project's synthetic calibration-step
    test fixtures already use elsewhere)."""
    base = _load("guide_center")
    shifted = np.roll(base, shift=(0, shift_px), axis=(0, 1))  # (dy, dx) -- pure +x shift

    result = measure_translation_offset(base, shifted)

    assert result is not None
    assert result.dx_px == float(shift_px)
    assert result.dy_px == 0.0
    assert result.score > 0.9  # exact self-shift, no reason for anything less


def test_mains_finer_plate_scale_shows_a_larger_real_shift_than_guides() -> None:
    """Cross-camera sanity check: AXIS2's real motion shows up on *both*
    cameras this run (unlike AXIS1's), and Main's own pixel shift for
    that same real pulse is larger than Guide's -- matching this
    project's own established ~8.7x plate-scale-ratio finding (Main's
    optics resolve finer detail, so the same real motion covers more of
    its own pixels), not a measurement artifact."""
    guide_offset = measure_translation_offset(
        _load("guide_center"), _load("guide_axis2_positive")
    )
    main_offset = measure_translation_offset(_load("main_center"), _load("main_axis2_positive"))
    assert guide_offset is not None and main_offset is not None
    guide_mag = (guide_offset.dx_px**2 + guide_offset.dy_px**2) ** 0.5
    main_mag = (main_offset.dx_px**2 + main_offset.dy_px**2) ** 0.5
    assert guide_mag > 0
    assert main_mag > guide_mag
