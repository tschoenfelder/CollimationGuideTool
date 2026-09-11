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
from astrotool_core.target.translation_offset import (
    TranslationOffset,
    measure_translation_offset,
)
from astrotool_core.testing.shift_grid import KNOWN_SHIFT_GRID, ShiftCase

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
    that exposed the bug, not just a synthetic reconstruction.

    Pinned to the exact real values (not just "is not None"): the full-
    resolution correlation peak's own precise location -- computed
    directly against these same real frames while designing the
    _FULL_RES_AGREEMENT_TOLERANCE_PX refinement -- agrees with the
    validated x8 answer (axis1 exactly; axis2 within 11px), so this
    regresses to whole-pixel precision (axis1: -168, -136; axis2: -315,
    426), not the coarser x8-block-quantized position (axis2 would
    otherwise round to -304, 416)."""
    before = _load_noisy_main(f"{axis_name}_before_left")
    after = _load_noisy_main(f"{axis_name}_after_left")

    offset = measure_translation_offset(before, after)

    assert offset is not None
    expected = {"axis1": (-168.0, -136.0), "axis2": (-315.0, 426.0)}[axis_name]
    assert (offset.dx_px, offset.dy_px) == expected


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


# ---------------------------------------------------------------------------
# af7d27b7 -- terrestrial "Run Calibration" + a nudge, both cameras severely
# underexposed (2.0 ms; the bracket runs with auto-exposure paused).
# Diagnostic af7d27b7-7216-4421-a993-a63cbd111822, git commit 7234354.
# Full writeup + per-frame map:
# local_test_data/af7d27b7_terrestrial_calibration_2026-09-10/README.md
# ---------------------------------------------------------------------------

_AF7D27B7_DIR = (
    Path(__file__).resolve().parents[2]
    / "local_test_data"
    / "af7d27b7_terrestrial_calibration_2026-09-10"
    / "frames"
)


def _load_af7d27b7(name: str) -> np.ndarray:
    data = fits.getdata(_AF7D27B7_DIR / f"{name}.fits")
    return np.asarray(data, dtype=np.float32)


@pytest.mark.skipif(
    not _AF7D27B7_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_AF7D27B7_DIR}",
)
@pytest.mark.parametrize(
    ("axis_name", "expected"),
    [
        ("axis1", (0.0, 402.0)),
        ("axis2", (469.0, 0.0)),
        ("nudge", (0.0, -208.0)),
    ],
)
def test_af7d27b7_guide_pairs_still_recover_their_recorded_shifts(
    axis_name: str, expected: tuple[float, float]
) -> None:
    """Guide (GPCMOS02000KPA) had just enough real signal for all three
    pulses. These exact displacements match `incident.json`'s own
    `calibration.right` (axis1/axis2) and `last_result.right` (nudge)
    entries -- a regression guard that a future estimator change must
    keep reproducing them from the real frames."""
    before = _load_af7d27b7(f"guide_{axis_name}_before")
    after = _load_af7d27b7(f"guide_{axis_name}_after")

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert (offset.dx_px, offset.dy_px) == expected


@pytest.mark.skipif(
    not _AF7D27B7_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_AF7D27B7_DIR}",
)
@pytest.mark.xfail(
    strict=False,
    reason="issue #28 known gap: Main (ATR585M) frames from this terrestrial "
    "calibration bracket are captured at 2.0 ms / gain 769 with almost no real "
    "signal (heavy uniform sensor grain, a faint large-scale gradient). The "
    "user reports a recognisable pattern and expects a shift to be detected "
    "here; the estimator currently returns None (axis1/axis2) or a confident "
    "(0, 0) (nudge, though the bright point source visibly leaves the frame). "
    "Flips to XPASS when the calibration flow reaches an adequate per-target "
    "exposure before the bracket, or the estimator gets more sensitive.",
)
@pytest.mark.parametrize("axis_name", ["axis1", "axis2", "nudge"])
def test_af7d27b7_main_pairs_should_yield_a_usable_nonzero_shift(axis_name: str) -> None:
    before = _load_af7d27b7(f"main_{axis_name}_before")
    after = _load_af7d27b7(f"main_{axis_name}_after")

    offset = measure_translation_offset(before, after)

    assert offset is not None
    assert (offset.dx_px, offset.dy_px) != (0.0, 0.0)


# ---------------------------------------------------------------------------
# 12bea18a -- terrestrial "Run Calibration" that failed both cameras
# ("in terrestrial mode stars should be usable for movement
# identification"). Diagnostic 12bea18a-47f2-4089-a6df-3ef9796fae97, git
# commit 7234354. Frame numbers are the bundle's own frame_N.fits:
# 6/8, 7/9     = axis1 before/after, left=Main / right=Guide
# 10/12, 11/13 = axis2 before/after, left=Main / right=Guide
# 14/16, 15/17 = nudge before/after, left=Main / right=Guide
# Full writeup:
# local_test_data/12bea18a_terrestrial_star_features_2026-09-10/README.md
# ---------------------------------------------------------------------------

_12BEA18A_DIR = (
    Path(__file__).resolve().parents[2]
    / "local_test_data"
    / "12bea18a_terrestrial_star_features_2026-09-10"
    / "frames"
)

_12BEA18A_PAIRS = {
    "main_axis1": ("frame_6", "frame_8"),
    "guide_axis1": ("frame_7", "frame_9"),
    "main_axis2": ("frame_10", "frame_12"),
    "guide_axis2": ("frame_11", "frame_13"),
    "main_nudge": ("frame_14", "frame_16"),
    "guide_nudge": ("frame_15", "frame_17"),
}


def _measure_12bea18a(pair: str) -> TranslationOffset | None:
    before_name, after_name = _12BEA18A_PAIRS[pair]
    before = np.asarray(fits.getdata(_12BEA18A_DIR / f"{before_name}.fits"), dtype=np.float32)
    after = np.asarray(fits.getdata(_12BEA18A_DIR / f"{after_name}.fits"), dtype=np.float32)
    return measure_translation_offset(before, after)


@pytest.mark.skipif(
    not _12BEA18A_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_12BEA18A_DIR}",
)
@pytest.mark.parametrize(
    ("pair", "expected"),
    [
        ("guide_axis2", (319.0, 2.0)),
        ("guide_nudge", (-112.0, 0.0)),
    ],
)
def test_12bea18a_guide_pairs_with_real_signal_still_match(
    pair: str, expected: tuple[float, float]
) -> None:
    """Guide's AXIS2 and nudge pulses carried just enough signal for a
    real measurement -- these match `incident.json`
    `calibration.right.axis2` and `last_result.right` exactly. Regression
    guard against a silent change to that behaviour."""
    offset = _measure_12bea18a(pair)
    assert offset is not None
    assert (offset.dx_px, offset.dy_px) == expected


@pytest.mark.skipif(
    not _12BEA18A_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_12BEA18A_DIR}",
)
@pytest.mark.xfail(
    strict=False,
    reason="issue #28 known gap: Guide's AXIS1 pulse is real, but its frames "
    "are underexposed enough (0.5 ms / gain 100) that the estimator returns a "
    "confident (0, 0) -- the 'false zero' that made calibration.right "
    "degenerate (last_failure_classes.right = calibration_invalid). Should "
    "recover a real non-zero shift; flips to XPASS when it does.",
)
def test_12bea18a_guide_axis1_should_not_be_a_false_zero() -> None:
    offset = _measure_12bea18a("guide_axis1")
    assert offset is not None
    assert (offset.dx_px, offset.dy_px) != (0.0, 0.0)


@pytest.mark.skipif(
    not _12BEA18A_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_12BEA18A_DIR}",
)
@pytest.mark.xfail(
    strict=False,
    reason="issue #28 known gap: every Main pair from this bracket returns "
    "None (0.5-1.0 ms / gain 100 -- sensor minimum, featureless at the pixel "
    "level). The user's premise is that faint stars in these frames should be "
    "usable for movement identification; flips to XPASS when the calibration "
    "flow reaches an adequate exposure before the bracket.",
)
@pytest.mark.parametrize("pair", ["main_axis1", "main_axis2", "main_nudge"])
def test_12bea18a_main_pairs_should_yield_a_usable_nonzero_shift(pair: str) -> None:
    offset = _measure_12bea18a(pair)
    assert offset is not None
    assert (offset.dx_px, offset.dy_px) != (0.0, 0.0)


# ---------------------------------------------------------------------------
# 28_corpus known-shift grid -- issues #28 (textured terrestrial) and #32
# (sparse star/point-source), same shared deterministic grid
# (astrotool_core.testing.shift_grid.KNOWN_SHIFT_GRID), applied via
# numpy.roll to two different real base frames: a textured Guide frame
# (known_shift/guide_base.fits) and a real Guide star frame (star/
# guide_star_base.fits, the same frame local_test_data/28_corpus/README.md
# documents as 1 real detected source). Both are 1920x1080 -- shifts beyond
# max_unaliased_shift_px((1080, 1920)) == (960, 540) are EXPECTED to read
# back as their exact circular alias, not the applied value: this is a
# property of any content this module measures at a shift this large
# relative to the frame (see max_unaliased_shift_px's own docstring for the
# real evidence), not a #32-specific sparse-content gap.
# ---------------------------------------------------------------------------

_28_CORPUS_DIR = Path(__file__).resolve().parents[2] / "local_test_data" / "28_corpus"
_KNOWN_SHIFT_DIR = _28_CORPUS_DIR / "known_shift"
_STAR_DIR = _28_CORPUS_DIR / "star"

_28_CORPUS_SKIP = pytest.mark.skipif(
    not _28_CORPUS_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_28_CORPUS_DIR}",
)

_28_SHAPE = (1080, 1920)  # Guide


def _load_28(path: Path) -> np.ndarray:
    return np.asarray(fits.getdata(path), dtype=np.float32)


def _expected_wrapped(applied: int, dimension: int) -> float:
    """What `measure_translation_offset()` reports for an `applied` shift
    along one axis of a frame `dimension` pixels wide/tall, accounting for
    `_correlate`'s own circular-shift unwrap -- see `max_unaliased_shift_px`'s
    docstring. Verified against this exact real corpus while building it:
    applied (0, 640) on this 1080-tall frame measured back as exactly
    (0, -440); this formula reproduces that (`640 % 1080 == 640`,
    `640 > 540` so `640 - 1080 == -440`)."""
    wrapped = applied % dimension
    return float(wrapped if wrapped <= dimension // 2 else wrapped - dimension)


def _assert_grid_case_matches(offset: TranslationOffset | None, case: ShiftCase) -> None:
    assert offset is not None, case.name
    assert offset.dx_px == _expected_wrapped(case.dx, _28_SHAPE[1]), case.name
    assert offset.dy_px == _expected_wrapped(case.dy, _28_SHAPE[0]), case.name


@_28_CORPUS_SKIP
@pytest.mark.parametrize(
    "case",
    [case for case in KNOWN_SHIFT_GRID if not (case.dx == 0 and case.dy == 0)],
    ids=lambda case: case.name,
)
def test_28_corpus_textured_known_shift_grid_matches_or_correctly_aliases(
    case: ShiftCase,
) -> None:
    """Issue #28's full 0-1000px grid against a real textured Guide frame.
    Every case recovers exactly, or (only past max_unaliased_shift_px)
    its exact circular alias -- never a genuinely wrong displacement."""
    before = _load_28(_KNOWN_SHIFT_DIR / "guide_base.fits")
    after = _load_28(_KNOWN_SHIFT_DIR / f"guide_roll_{case.name}.fits")

    _assert_grid_case_matches(measure_translation_offset(before, after), case)


@_28_CORPUS_SKIP
def test_28_corpus_textured_known_shift_zero_is_high_confidence_zero() -> None:
    before = _load_28(_KNOWN_SHIFT_DIR / "guide_base.fits")

    offset = measure_translation_offset(before, before.copy())

    assert offset is not None
    assert (offset.dx_px, offset.dy_px) == (0.0, 0.0)
    assert offset.score > 0.9


@_28_CORPUS_SKIP
@pytest.mark.parametrize(
    "case",
    [case for case in KNOWN_SHIFT_GRID if not (case.dx == 0 and case.dy == 0)],
    ids=lambda case: case.name,
)
def test_32_corpus_star_content_known_shift_grid_matches_or_correctly_aliases(
    case: ShiftCase,
) -> None:
    """Issue #32: the SAME grid against a real Guide STAR frame (1 real
    detected source, see local_test_data/28_corpus/README.md) -- proves
    the existing, unmodified estimator already handles sparse/point-source
    content through the same one interface used for textured terrestrial
    content, exactly matching the textured test above case-for-case. No new
    internal strategy was needed for this corpus (see this module's own
    TestSparseStarContent in tests/core/target/test_translation_offset.py
    for the synthetic characterization this real-frame evidence confirms)."""
    before = _load_28(_STAR_DIR / "guide_star_base.fits")
    after = _load_28(_STAR_DIR / f"guide_star_roll_{case.name}.fits")

    _assert_grid_case_matches(measure_translation_offset(before, after), case)


@_28_CORPUS_SKIP
def test_32_corpus_star_content_known_shift_zero_is_high_confidence_zero() -> None:
    before = _load_28(_STAR_DIR / "guide_star_base.fits")

    offset = measure_translation_offset(before, before.copy())

    assert offset is not None
    assert (offset.dx_px, offset.dy_px) == (0.0, 0.0)
    assert offset.score > 0.9
