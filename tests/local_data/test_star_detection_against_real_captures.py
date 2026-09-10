"""detect_sources() tested against real hardware captures -- local-only,
never committed (the frames live in git-ignored `local_test_data/`).
Skipped entirely on any machine without the dataset present.

Two datasets, one root cause, both captured at git commit 7234354:

* ``08d264f8-2068-404b-acf3-ed93d777233d`` -- a star-mode "Run
  Calibration" that aborted with
  ``last_result.error = "no star detected before pulsing"``. User:
  "use these as test data for detecting stars in main and guide".
* ``12bea18a-47f2-4089-a6df-3ef9796fae97`` -- a terrestrial-mode "Run
  Calibration" that failed both cameras. User: "in terrestrial mode
  stars should be usable for movement identification".

In both, the calibration bracket captured every frame at the sensor
minimum (0.5-1.0 ms, gain 100) with auto-exposure *paused* for the whole
bracket, so there is almost no real signal: Main sits at ~2.5 % of its
16-bit range, Guide's max is ~50-100 of 4095. ``detect_sources`` calling
these ``too_dark`` is defensible; the user's position is that faint stars
are present and should be usable. The likely enhancement is upstream --
the bracket should reach an adequate per-target exposure/gain before
capturing -- not in ``detect_sources`` itself (shared theme with
``af7d27b7`` in test_translation_offset_against_real_captures.py).

The plain tests here pin the CURRENT counts as a regression baseline
(same convention as the other two files in this directory). The
``xfail(strict=False)`` tests assert the behaviour the user expects; each
flips to XPASS when the exposure-flow / sensitivity gap closes.

See ``local_test_data/08d264f8_star_detection_2026-09-10/README.md`` and
``local_test_data/12bea18a_terrestrial_star_features_2026-09-10/README.md``
for the full per-frame writeups.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits
from astrotool_core.target.detector import detect_sources

_08D264F8_DIR = (
    Path(__file__).resolve().parents[2]
    / "local_test_data"
    / "08d264f8_star_detection_2026-09-10"
    / "frames"
)
_12BEA18A_DIR = (
    Path(__file__).resolve().parents[2]
    / "local_test_data"
    / "12bea18a_terrestrial_star_features_2026-09-10"
    / "frames"
)

_08_SKIP = pytest.mark.skipif(
    not _08D264F8_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_08D264F8_DIR}",
)
_12_SKIP = pytest.mark.skipif(
    not _12BEA18A_DIR.is_dir(),
    reason=f"real-hardware dataset not present locally at {_12BEA18A_DIR}",
)


def _load(dataset_dir: Path, name: str) -> np.ndarray:
    data = fits.getdata(dataset_dir / f"{name}.fits")
    return np.asarray(data, dtype=np.float32)


# --- 08d264f8: regression baseline (what the frames currently give) -------


@_08_SKIP
@pytest.mark.parametrize(
    "frame",
    # Main (3840x2160): recent x2, axis1_before, nudge before/after
    ["frame_0", "frame_1", "frame_6", "frame_14", "frame_16"],
)
def test_08d264f8_main_frames_are_currently_too_dark(frame: str) -> None:
    """Every Main frame in this bundle -- max ~16 000-25 000 of 65 535,
    p99.9 ~1650-2800 -- currently yields no sources and image_quality
    'too_dark'. Pins that as the baseline the xfail below is measured
    against."""
    result = detect_sources(_load(_08D264F8_DIR, frame), exposure_s=0.5, gain=100)

    assert result.sources == ()
    assert result.image_quality == "too_dark"


@_08_SKIP
# Guide (1920x1080): recent x2, axis1_before
@pytest.mark.parametrize("frame", ["frame_3", "frame_4", "frame_7"])
def test_08d264f8_guide_dark_frames_yield_nothing(frame: str) -> None:
    result = detect_sources(_load(_08D264F8_DIR, frame), exposure_s=0.5, gain=100)

    assert result.sources == ()
    assert result.image_quality == "too_dark"


@_08_SKIP
def test_08d264f8_guide_nudge_before_finds_one_star() -> None:
    """The only genuine detection anywhere in this bundle: Guide's
    nudge_before frame (frame_15, max 101 of 4095, p99.9 16) -> exactly
    one `normal_star`. Regression guard for the estimator's low-signal
    floor -- a change that loses this or invents more should fail here."""
    result = detect_sources(_load(_08D264F8_DIR, "frame_15"), exposure_s=0.001, gain=100)

    assert len(result.sources) == 1
    assert result.sources[0].kind == "normal_star"
    assert result.sources[0].peak > 0


@_08_SKIP
def test_08d264f8_guide_nudge_after_finds_four_sources() -> None:
    """Guide's nudge_after frame (frame_17) -> 4 sources: 2 normal_star,
    2 distorted_star. Pins the count and the kind multiset."""
    result = detect_sources(_load(_08D264F8_DIR, "frame_17"), exposure_s=0.001, gain=100)

    assert len(result.sources) == 4
    assert sorted(source.kind for source in result.sources) == [
        "distorted_star",
        "distorted_star",
        "normal_star",
        "normal_star",
    ]


# --- 08d264f8: the gap the user is reporting -----------------------------


@_08_SKIP
@pytest.mark.xfail(
    strict=False,
    reason='This is the exact frame behind last_result.error = "no star '
    'detected before pulsing": Guide\'s axis1_before (frame_7). The user '
    "expects a usable star here. Currently 0 sources / 'too_dark' because the "
    "star-mode calibration bracket captured at 0.5 ms / gain 100. Flips to "
    "XPASS when the bracket reaches an adequate exposure before capturing.",
)
def test_08d264f8_guide_axis1_before_should_detect_a_star() -> None:
    result = detect_sources(_load(_08D264F8_DIR, "frame_7"), exposure_s=0.0005, gain=100)

    assert len(result.sources) >= 1


@_08_SKIP
@pytest.mark.xfail(
    strict=False,
    reason="User: 'test data for detecting stars in main and guide'. Main's "
    "own live-recent frame (frame_0) currently yields nothing -- see the "
    "module docstring for why. Flips to XPASS when Main reaches a usable "
    "exposure/gain for star detection.",
)
def test_08d264f8_main_recent_should_detect_a_star() -> None:
    result = detect_sources(_load(_08D264F8_DIR, "frame_0"), exposure_s=0.5, gain=100)

    assert len(result.sources) >= 1


# --- 12bea18a: regression baseline + the same gap ------------------------


@_12_SKIP
# Main: recent, axis1_before, nudge_before
@pytest.mark.parametrize("frame", ["frame_0", "frame_6", "frame_14"])
def test_12bea18a_main_frames_are_currently_too_dark(frame: str) -> None:
    result = detect_sources(_load(_12BEA18A_DIR, frame), exposure_s=0.5, gain=100)

    assert result.sources == ()
    assert result.image_quality == "too_dark"


@_12_SKIP
@pytest.mark.parametrize("frame", ["frame_3", "frame_7"])  # Guide recent, axis1_before
def test_12bea18a_guide_dark_frames_yield_nothing(frame: str) -> None:
    result = detect_sources(_load(_12BEA18A_DIR, frame), exposure_s=0.5, gain=100)

    assert result.sources == ()
    assert result.image_quality == "too_dark"


@_12_SKIP
def test_12bea18a_guide_nudge_before_finds_one_star() -> None:
    """Guide's nudge_before (frame_15, max 101, p99.9 16) -> one
    `normal_star`, the single real detection in this bundle."""
    result = detect_sources(_load(_12BEA18A_DIR, "frame_15"), exposure_s=0.001, gain=100)

    assert len(result.sources) == 1
    assert result.sources[0].kind == "normal_star"


@_12_SKIP
@pytest.mark.xfail(
    strict=False,
    reason="User: 'in terrestrial mode stars should be usable for movement "
    "identification'. Guide's axis1_before (frame_7) currently yields nothing, "
    "which is what made calibration.right.axis1 a false (0, 0) -> degenerate "
    "matrix -> calibration_invalid. Flips to XPASS when a faint star here "
    "becomes detectable.",
)
def test_12bea18a_guide_axis1_before_should_detect_a_star() -> None:
    result = detect_sources(_load(_12BEA18A_DIR, "frame_7"), exposure_s=0.0005, gain=100)

    assert len(result.sources) >= 1


@_12_SKIP
@pytest.mark.xfail(
    strict=False,
    reason="Main (ATR585M) is featureless at the pixel level in this bracket "
    "(0.5 ms / gain 100). 'Stars should be usable for movement identification' "
    "-- flips to XPASS when Main reaches a usable exposure.",
)
def test_12bea18a_main_recent_should_detect_a_star() -> None:
    result = detect_sources(_load(_12BEA18A_DIR, "frame_0"), exposure_s=0.5, gain=100)

    assert len(result.sources) >= 1
