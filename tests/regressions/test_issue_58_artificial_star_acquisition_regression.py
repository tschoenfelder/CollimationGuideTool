"""Issue #58 -- artificial-star autofocus with several heavily over-exposed stars, on the REAL
Main frames of diagnostic 4c5677d0-c97a-4a5a-9a10-86d8f71c95dd (`datasets/regressions/58/`):
four artificial stars, each a clipped plateau ~130 px across with a wide halo, at 2000 ms.

LOCAL-ONLY: the frames are not committed (large test data stays local); they live in the
git-ignored `local_test_data/58/frames/` and every test here skips, with the reason, when they
are absent (CI). The logic is covered without them by the synthetic tests in
tests/core/focus/test_star_candidates.py, tests/core/focus/test_star_target_tracker.py and
tests/collimation/application/test_autofocus_controller_artificial_star.py.

Before the fix `detect_sources` returned 785 sources for the four blobs and
`StarTargetTracker.acquire` refused anything but exactly one (`multiple_candidates`), so the
run ended on the first frame. Expected values (the four plateau centres, by a separate flood
fill of the clipped pixels) are in expected.json, independent of the grouping code.

Bespoke (not the generic `_loader.BOUNDARIES` driver): the cases need the tracker and the
controller, not a frame-pair boundary. `detect_sources` takes ~10 s per full frame here, so
it runs once per frame (module-scoped cache).
"""

from __future__ import annotations

import math
from functools import cache

import numpy as np
import pytest
from _loader import REGRESSIONS_DIR, REPO_ROOT, RegressionCase, load_manifest
from astropy.io import fits
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.star_candidates import group_star_candidates
from astrotool_core.focus.star_focus_metric import StarTargetTracker
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.point_source import PointSource
from astrotool_core.timing import FakeClock
from collimation_tool.application.autofocus_controller import (
    SATURATED_AT_MIN_EXPOSURE,
    AutofocusController,
    AutofocusMode,
    ExposureControl,
)
from collimation_tool.application.autofocus_search import AutofocusStatus

_DATASET = "58"
_CASES = tuple(load_manifest(REGRESSIONS_DIR / _DATASET).cases)


@cache
def _frame(relative: str) -> np.ndarray:
    path = REPO_ROOT / relative
    if not path.is_file():
        pytest.skip(
            f"local-only real frame absent: {path} -- see datasets/regressions/{_DATASET}/"
            "README.md (pull_diagnostic_bundle.py + slim_regression_frames.py)"
        )
    return np.asarray(fits.getdata(path), dtype=np.float64)


@cache
def _sources(relative: str) -> tuple[PointSource, ...]:
    return detect_sources(_frame(relative)).sources


def _near(point: tuple[float, float], expected: list[float], tolerance: float) -> bool:
    return math.hypot(point[0] - expected[0], point[1] - expected[1]) <= tolerance


@pytest.mark.parametrize("case", _CASES, ids=[case.name for case in _CASES])
def test_the_four_blobs_are_four_candidates(case: RegressionCase) -> None:
    sources = _sources(case.inputs["frame"])
    assert len(sources) > 100  # the detector's fragments (785 on frame_0): the defect's input

    candidates = group_star_candidates(_frame(case.inputs["frame"]), sources)

    tolerance = case.tolerances["centre_px"]
    assert len(candidates) == case.expect["candidates"]
    for centre in case.expect["plateau_centres"]:
        assert sum(_near((c.x, c.y), centre, tolerance) for c in candidates) == 1, centre
    assert all(c.saturated for c in candidates)


@pytest.mark.parametrize("case", _CASES, ids=[case.name for case in _CASES])
def test_the_star_nearest_the_centre_is_acquired_and_reported(
    case: RegressionCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    frame = _frame(case.inputs["frame"])
    sources = _sources(case.inputs["frame"])
    # The tracker's own detect_sources call, served from the per-frame cache (same result).
    monkeypatch.setattr(
        "astrotool_core.focus.star_focus_metric.detect_sources",
        lambda image: type("R", (), {"sources": sources})(),
    )
    tracker = StarTargetTracker()

    result = tracker.acquire(frame)

    assert result.reason == ("saturated" if case.expect["target_saturated"] else None)
    assert tracker.candidate_count == case.expect["candidates"]
    assert tracker.target is not None
    assert _near(tracker.target, case.expect["target"], case.tolerances["centre_px"])


def test_the_run_on_the_real_frame_follows_the_centre_star_and_says_it_is_clipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end through the real controller + searcher: the camera serves the real frame
    and is already at its minimum exposure (the clipped frame cannot be re-exposed off-rig),
    so the run must end `saturated_at_min_exposure` on the centre star -- not
    `multiple_candidates` -- with the exposure untouched and the focuser not moved."""
    case = _CASES[0]
    frame = _frame(case.inputs["frame"])
    sources = _sources(case.inputs["frame"])
    monkeypatch.setattr(
        "astrotool_core.focus.star_focus_metric.detect_sources",
        lambda image: type("R", (), {"sources": sources})(),
    )
    clock = FakeClock()
    focuser = FakeFocuser()
    focuser.move_absolute(14780)
    settings = {"exposure": (2000.0, 460)}

    def wait_for_frame(reference: float, timeout: float) -> FrameAcquisitionResult:
        return FrameAcquisitionResult(
            status=FrameAcquisitionStatus.OK,
            frame=DeliveredFrame(
                pixels=frame, captured_at_monotonic=clock.monotonic(), exposure_seconds=2.0
            ),
        )

    def set_exposure(exposure_ms: float, gain: int) -> None:
        settings["exposure"] = (max(exposure_ms, 2000.0), gain)  # already at the minimum

    controller = AutofocusController(
        focuser,
        get_frame=lambda: frame,
        wait_for_frame=wait_for_frame,
        set_auto_exposure_paused=lambda paused: None,
        exposure_control=ExposureControl(get=lambda: settings["exposure"], set=set_exposure),
        clock=clock,
    )

    result = controller.run(AutofocusMode.ARTIFICIAL_STAR)

    assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
    assert result.failure_reason == SATURATED_AT_MIN_EXPOSURE  # was multiple_candidates
    assert result.target_candidates == case.expect["candidates"]
    assert result.tracked_target is not None
    assert _near(result.tracked_target, case.expect["target"], case.tolerances["centre_px"])
    assert focuser.get_position() == 14780
    assert settings["exposure"] == (2000.0, 460)
