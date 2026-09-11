"""check_image_stability() tested against real hardware captures -- local-only,
never committed. Skipped entirely on any machine without the dataset present
(see tests/local_data/test_translation_offset_against_real_captures.py for the
same convention this file mirrors).

Validates issue #30's own stability-*math* (`check_image_stability`, built on
top of `measure_translation_offset`) against real pixel content: the
10-frame-per-camera stationary corpus, the real large-motion axis pairs
(`run1`/`run3`), and `run2`'s own still-unexplained false-zero pairs. This is
explicitly NOT an exercise of the live timing/waiting/settling/bounded-timeout
machinery `acquire_verified_frame`/`MountTestMovePanel._capture_both` add on
top of this math -- FITS files carry no exposure-start timestamps, no
monotonic capture times, no commanded-motion reference, so none of that is
reachable this way. This is a regression floor under the math only; real
`Run Calibration` attempts on the live rig remain the only way to validate the
live wiring and tune `stability_tolerance_px`/`stability_timeout_s` for real.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astrotool_core.acquisition.image_stability import StabilityStatus, check_image_stability

_28_CORPUS_DIR = Path(__file__).resolve().parents[2] / "local_test_data" / "28_corpus"
_STATIONARY_DIR = _28_CORPUS_DIR / "stationary"
_REAL_PAIRS_DIR = _28_CORPUS_DIR / "real_pairs"

pytestmark = pytest.mark.skipif(
    not _28_CORPUS_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_28_CORPUS_DIR}",
)


def _load(path: Path) -> np.ndarray:
    return np.asarray(fits.getdata(path), dtype=np.float32)


def _stationary_sequence(camera: str) -> list[np.ndarray]:
    return [_load(_STATIONARY_DIR / f"{camera}_{i:02d}.fits") for i in range(10)]


def _real_pair(run: str, camera: str, axis: str) -> tuple[np.ndarray, np.ndarray]:
    before = _load(_REAL_PAIRS_DIR / f"{run}_{camera}_{axis}_before.fits")
    after = _load(_REAL_PAIRS_DIR / f"{run}_{camera}_{axis}_after.fits")
    return before, after


# ---------------------------------------------------------------------------
# Stationary corpus (local_test_data/28_corpus/stationary/, captured
# 2026-09-11 -- see that directory's own README.md "2026-09-11 follow-up"
# section): 10 real consecutive frames per camera, mount confirmed untouched
# (IndiMountParkAdapter.status() read parked=False, tracking=False
# immediately before capture) via a direct TouptekCameraAdapter connection,
# same class the app itself uses. Full C(10,2)=45-pair sweep (via
# scripts/translation_estimator_benchmark.py, not this file's own
# consecutive-pair-only check): Guide read exact (0,0) on all 45 pairs;
# Main showed real, small, non-mount-motion drift (max 5.00px over any gap,
# median 2.00px) -- most likely outdoor daytime scene content genuinely
# changing slightly (wind-blown foliage) over the ~15-20s the sequence took,
# not tracking drift (mount was confirmed untracked, and Guide's own perfect
# zero record across the same window rules out real mount motion entirely).
# ---------------------------------------------------------------------------


def test_guide_stationary_sequence_reads_stable_even_at_a_tight_tolerance() -> None:
    """Guide's own worst real consecutive-pair displacement across this
    corpus is exactly 0.0px (see this directory's README) -- STABLE even
    at a tolerance far tighter than `stability_tolerance_px`'s own 3.0px
    default."""
    result = check_image_stability(_stationary_sequence("guide"), tolerance_px=0.5)

    assert result.status is StabilityStatus.STABLE
    assert result.max_displacement_px == 0.0


def test_main_stationary_sequence_reads_unstable_at_a_tight_tolerance() -> None:
    """Main's own real environmental drift (max 4.47px between any two
    *consecutive* frames in this sequence -- narrower than the corpus's
    own 5.00px worst-case across every possible pairing, which includes
    wider time gaps) reads as genuinely UNSTABLE at a tolerance tighter
    than that real drift -- confirms this isn't a check that trivially
    reports STABLE regardless of content."""
    result = check_image_stability(_stationary_sequence("main"), tolerance_px=1.0)

    assert result.status is StabilityStatus.UNSTABLE
    assert result.max_displacement_px == pytest.approx(4.47213595499958)


def test_main_stationary_sequence_reads_stable_at_the_derived_tolerance() -> None:
    """At `stability_tolerance_px`'s own real-evidence-derived default
    (3.0px, see `_DEFAULT_STABILITY_TOLERANCE_PX`'s own docstring) this
    specific 10-frame sequence's own worst consecutive-pair displacement
    (4.47px) would still read UNSTABLE -- this pins the *next* threshold
    up (6.0px, matching `datasets/regressions/28/expected.json`'s own
    real-evidence-derived stationary tolerance) instead, the concrete
    number this real corpus should inform `stability_tolerance_px`'s own
    tuning toward, pending a live "Run Calibration" attempt to confirm it
    doesn't also mask genuine instability."""
    result = check_image_stability(_stationary_sequence("main"), tolerance_px=6.0)

    assert result.status is StabilityStatus.STABLE


# ---------------------------------------------------------------------------
# Real motion pairs (local_test_data/28_corpus/real_pairs/run{1,3}_*) -- real,
# large, intentional axis pulses from two successful real calibration runs
# (`c17bb07b`/`8ce36f8a`). A coarse sanity check only: these are 2-frame
# before/after pairs, not a `stability_sample_count`-frame sequence, so this
# doesn't exercise the real sliding-window algorithm -- it confirms the
# underlying math never mistakes a real, large commanded displacement for
# stability.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("run", ["run1", "run3"])
@pytest.mark.parametrize("camera", ["main", "guide"])
@pytest.mark.parametrize("axis", ["axis1", "axis2"])
def test_real_motion_pairs_never_read_stable(run: str, camera: str, axis: str) -> None:
    """Every one of these 8 real pairs measured well above 20px of real
    displacement (see the diagnostic-bundle evidence this corpus was
    pulled from) -- never STABLE, even at a generous tolerance far looser
    than `stability_tolerance_px`'s own default."""
    before, after = _real_pair(run, camera, axis)

    result = check_image_stability([before, after], tolerance_px=10.0)

    assert result.status is not StabilityStatus.STABLE


# ---------------------------------------------------------------------------
# run2's own false-zero pairs (`859f2520-a35b-490d-9d5f-cd6fde68aaf9`,
# "second calibration run", FAILED -- both Main axes false-zero, Guide axis2
# false-zero, still UNEXPLAINED despite investigation -- see this corpus's
# own README "The false-zero mystery" section). Pinning whatever
# check_image_stability actually reports, verified directly against these
# real frames, not assumed.
#
# IMPORTANT: this does NOT explain or close the 859f2520 incident. A false
# zero here means the two frames a real pulse straddled measure as
# identical -- indistinguishable, by this math alone, from a camera that
# never moved at all. Whatever root cause produced that (mount/driver,
# environmental, or something else) is issue #28's own territory, not #30's;
# this only confirms the stability layer doesn't itself misclassify these
# specific real frames one way or the other.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("camera", "axis", "expected_status"),
    [
        ("main", "axis1", StabilityStatus.STABLE),
        ("main", "axis2", StabilityStatus.STABLE),
        ("guide", "axis1", StabilityStatus.UNSTABLE),
        ("guide", "axis2", StabilityStatus.STABLE),
    ],
)
def test_run2_false_zero_pairs_pinned_against_real_frames(
    camera: str, axis: str, expected_status: StabilityStatus
) -> None:
    before, after = _real_pair("run2", camera, axis)

    result = check_image_stability([before, after], tolerance_px=3.0)

    assert result.status is expected_status
