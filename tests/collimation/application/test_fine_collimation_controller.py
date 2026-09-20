"""Tests for FineCollimationController — issue #21 (Stage 7): the first
end-to-end orchestration of Stages 1→2→3→4→5→6 together, live-feeding a
scripted `get_frame` through a real `FocusedStarAcquisition` to produce
one bundled `FineCollimationResult`. Guide-camera-assisted reacquisition
is explicitly out of scope for this pass (see the issue #21 plan) — a
star that can't be found in the Main frame within the acquisition's own
bounded retries is reported as a clean failure, not chased via the
guide camera."""

from __future__ import annotations

import numpy as np
from astrotool_core.diffraction.optical_reference_model import OpticalConfig
from astrotool_core.diffraction.symmetry_measurement import SymmetryStatus
from astrotool_core.testing.frame_factory import airy_pattern_image
from collimation_tool.application.fine_collimation_controller import (
    FineCollimationController,
    GetFrame,
    GuideReacquirer,
)
from collimation_tool.application.star_acquisition import (
    AcquisitionResult,
    AcquisitionStatus,
    FocusedStarAcquisition,
)
from collimation_tool.domain.target_mode import CollimationTargetMode

_SHAPE = (140, 140)
_STAR_X, _STAR_Y = 70.0, 70.0
_ROI_SIZE = (120, 120)


def _star_frame(rng: np.random.Generator, *, jitter_px: float = 1.5) -> np.ndarray:
    dx = float(rng.uniform(-jitter_px, jitter_px))
    dy = float(rng.uniform(-jitter_px, jitter_px))
    frame = airy_pattern_image(
        _SHAPE, x=_STAR_X + dx, y=_STAR_Y + dy, peak=5000.0, core_sigma=2.0, background=100.0,
        ring_radius_px=12.0, ring_peak=1200.0, ring_sigma=1.5,
    )
    return frame + rng.normal(0.0, 10.0, size=_SHAPE)


def _blank_frame() -> np.ndarray:
    return np.full(_SHAPE, 100.0) + np.random.default_rng(0).normal(0.0, 5.0, size=_SHAPE)


def _make_controller(
    *, sample_count: int = 4, get_frame: GetFrame, acquisition: FocusedStarAcquisition | None = None
) -> FineCollimationController:
    return FineCollimationController(
        acquisition or FocusedStarAcquisition(roi_size=_ROI_SIZE),
        get_frame=get_frame,
        optical_config=OpticalConfig(),
        sample_count=sample_count,
    )


class TestEndToEndOrchestration:
    def test_a_well_tracked_star_produces_a_full_fine_collimation_result(self) -> None:
        rng = np.random.default_rng(1)
        controller = _make_controller(sample_count=4, get_frame=lambda: _star_frame(rng))

        outcome = controller.run()

        assert outcome.status == "success"
        assert outcome.reason is None
        assert outcome.result is not None
        assert outcome.result.stack_result.stacked is not None
        assert outcome.result.stack_result.frame_count == 4
        assert outcome.result.profile_result.sufficient_sampling is True
        assert outcome.result.symmetry_result.status is not SymmetryStatus.INVALID


class TestBoundedFailureModes:
    def test_no_star_ever_found_reports_a_clean_star_lost_failure(self) -> None:
        # select() on a starless frame reports LOST immediately (the
        # same underlying AcquisitionStatus as losing an already-tracked
        # star mid-collection, below) -- one reason covers both, since
        # FocusedStarAcquisition itself doesn't distinguish them.
        controller = _make_controller(sample_count=4, get_frame=_blank_frame)

        outcome = controller.run()

        assert outcome.status == "failed"
        assert outcome.result is None
        assert outcome.reason == "star_lost"

    def test_star_lost_mid_collection_reports_a_clean_failure_not_a_hang(self) -> None:
        rng = np.random.default_rng(2)
        frames = iter([_star_frame(rng), *[_blank_frame() for _ in range(10)]])

        acquisition = FocusedStarAcquisition(
            roi_size=_ROI_SIZE, max_full_frame_search_attempts=2
        )
        controller = _make_controller(
            sample_count=4,
            get_frame=lambda: next(frames, _blank_frame()),
            acquisition=acquisition,
        )

        outcome = controller.run()

        assert outcome.status == "failed"
        assert outcome.result is None
        assert outcome.reason == "star_lost"

    def test_get_frame_returning_none_does_not_hang_and_eventually_fails(self) -> None:
        controller = _make_controller(sample_count=4, get_frame=lambda: None)

        outcome = controller.run()

        assert outcome.status == "failed"
        assert outcome.result is None

    def test_cancel_check_stops_the_run_promptly(self) -> None:
        rng = np.random.default_rng(3)
        calls = {"n": 0}

        def get_frame() -> np.ndarray:
            calls["n"] += 1
            return _star_frame(rng)

        controller = _make_controller(sample_count=1000, get_frame=get_frame)

        outcome = controller.run(cancel_check=lambda: calls["n"] > 3)

        assert outcome.status == "failed"
        assert outcome.reason == "cancelled"
        assert calls["n"] <= 6


class TestGuideAssistedReacquisition:
    """Issue #39: a star lost from Main mid-collection is recovered via the
    guide camera (an injected reacquirer) and the run resumes with the SAME
    tracked target -- never a fresh selection."""

    @staticmethod
    def _controller(
        reacquirer: GuideReacquirer | None,
        *,
        tail: np.ndarray | None = None,
        seed: int = 11,
    ) -> FineCollimationController:
        rng = np.random.default_rng(seed)
        iterator = iter([_star_frame(rng), _blank_frame(), _blank_frame()])
        return FineCollimationController(
            FocusedStarAcquisition(roi_size=_ROI_SIZE, max_full_frame_search_attempts=2),
            get_frame=lambda: next(iterator, tail if tail is not None else _star_frame(rng)),
            optical_config=OpticalConfig(),
            sample_count=4,
            target_mode=CollimationTargetMode.ARTIFICIAL_STAR,
            guide_reacquirer=reacquirer,
        )

    @staticmethod
    def _found_in_guide(
        acquisition: FocusedStarAcquisition, cancel_check: object
    ) -> AcquisitionResult:
        return AcquisitionResult(AcquisitionStatus.SEARCHING_GUIDE, None, (70.0, 70.0), None)

    def test_a_successful_reacquisition_resumes_and_completes_the_run(self) -> None:
        calls: list[int] = []

        def reacquirer(acq: FocusedStarAcquisition, cancel: object) -> AcquisitionResult:
            calls.append(1)
            return self._found_in_guide(acq, cancel)

        outcome = self._controller(reacquirer).run()

        assert calls == [1]
        assert outcome.status == "success"
        assert outcome.result is not None
        assert outcome.result.target_mode is CollimationTargetMode.ARTIFICIAL_STAR
        assert "reacquired_via_guide" in outcome.reacquisition_log

    def test_a_failed_reacquisition_reports_its_own_reason_not_a_generic_loss(self) -> None:
        def reacquirer(acq: FocusedStarAcquisition, cancel: object) -> AcquisitionResult:
            return AcquisitionResult(AcquisitionStatus.LOST, None, None, "target_not_found_guide")

        outcome = self._controller(reacquirer).run()

        assert outcome.status == "failed"
        assert outcome.reason == "target_not_found_guide"
        assert "guide_reacquisition_attempted" in outcome.reacquisition_log

    def test_an_ambiguous_return_to_main_never_silently_switches_target(self) -> None:
        def star(x: float) -> np.ndarray:
            return airy_pattern_image(
                _SHAPE, x=x, y=70.0, peak=5000.0, core_sigma=2.0, background=100.0,
                ring_radius_px=12.0, ring_peak=1200.0, ring_sigma=1.5,
            )

        two_stars = star(45.0) + star(95.0) - 100.0

        outcome = self._controller(self._found_in_guide, tail=two_stars).run()

        assert outcome.status == "failed"
        assert outcome.reason == "target_ambiguous"

    def test_without_a_reacquirer_a_lost_star_is_still_a_clean_star_lost(self) -> None:
        outcome = self._controller(None).run()

        assert outcome.status == "failed"
        assert outcome.reason == "star_lost"

    def test_the_first_selection_failing_never_triggers_guide_reacquisition(self) -> None:
        calls: list[int] = []

        def reacquirer(acq: FocusedStarAcquisition, cancel: object) -> AcquisitionResult:
            calls.append(1)
            return self._found_in_guide(acq, cancel)

        controller = FineCollimationController(
            FocusedStarAcquisition(roi_size=_ROI_SIZE),
            get_frame=_blank_frame,
            optical_config=OpticalConfig(),
            sample_count=4,
            guide_reacquirer=reacquirer,
        )

        outcome = controller.run()

        assert outcome.reason == "star_lost"
        assert calls == []
