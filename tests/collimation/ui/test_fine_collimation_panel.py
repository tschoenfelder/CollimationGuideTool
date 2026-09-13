"""Tests for FineCollimationPanel — issue #21 AC 7.1/7.2: the first
UI-bearing stage of the fine-collimation pipeline. Constructs the panel
directly (project convention, avoids the known Qt-teardown segfault
odds a MainWindow-based test raises) and feeds it a `FineCollimationOutcome`
directly via `show_outcome()`, bypassing the runner/threading entirely --
these are pure rendering-logic tests."""

from __future__ import annotations

from astrotool_core.diffraction.optical_reference_model import (
    OpticalConfig,
    compute_diffraction_reference,
)
from astrotool_core.diffraction.radial_profile import compute_radial_profile
from astrotool_core.diffraction.symmetry_measurement import (
    SymmetryStatus,
    compute_symmetry_measurement,
)
from astrotool_core.target.stacking import StackResult
from astrotool_core.testing.frame_factory import airy_pattern_image, coma_pattern_image
from collimation_tool.application.fine_collimation_controller import (
    FineCollimationOutcome,
    FineCollimationResult,
)
from collimation_tool.ui.fine_collimation_panel import FineCollimationPanel

_SHAPE = (120, 120)
_CENTER_X, _CENTER_Y = 60.0, 60.0


def _make_panel() -> FineCollimationPanel:
    return FineCollimationPanel(get_frame=lambda: None)


def _valid_fine_collimated_outcome() -> FineCollimationOutcome:
    stacked = airy_pattern_image(
        _SHAPE, x=_CENTER_X, y=_CENTER_Y, peak=5000.0, core_sigma=2.0, background=100.0,
        ring_radius_px=12.0, ring_peak=1200.0, ring_sigma=1.5,
    )
    profile_result = compute_radial_profile(stacked)
    assert profile_result.sufficient_sampling is True
    reference_result = compute_diffraction_reference(OpticalConfig())
    symmetry_result = compute_symmetry_measurement(stacked, profile_result, reference_result)
    assert symmetry_result.status is SymmetryStatus.FINE_COLLIMATED

    result = FineCollimationResult(
        stack_result=StackResult(stacked=stacked, frame_count=4, quality_scores=(1.0,) * 4),
        profile_result=profile_result,
        reference_result=reference_result,
        symmetry_result=symmetry_result,
    )
    return FineCollimationOutcome(status="success", result=result, reason=None)


def _insufficient_sampling_outcome() -> FineCollimationOutcome:
    stacked = airy_pattern_image(
        (60, 80), x=30.0, y=25.0, peak=3000.0, core_sigma=1.0, background=100.0,
    )
    profile_result = compute_radial_profile(stacked, min_fwhm_px=3.0)
    assert profile_result.sufficient_sampling is False
    reference_result = compute_diffraction_reference(OpticalConfig())
    symmetry_result = compute_symmetry_measurement(stacked, profile_result, reference_result)
    assert symmetry_result.status is SymmetryStatus.INVALID

    result = FineCollimationResult(
        stack_result=StackResult(stacked=stacked, frame_count=4, quality_scores=(1.0,) * 4),
        profile_result=profile_result,
        reference_result=reference_result,
        symmetry_result=symmetry_result,
    )
    return FineCollimationOutcome(status="success", result=result, reason=None)


def _low_confidence_outcome() -> FineCollimationOutcome:
    stacked = airy_pattern_image(
        _SHAPE, x=_CENTER_X, y=_CENTER_Y, peak=5000.0, core_sigma=2.0, background=100.0,
        ring_radius_px=12.0, ring_peak=5.0, ring_sigma=1.5,
    )
    profile_result = compute_radial_profile(stacked)
    assert profile_result.sufficient_sampling is True
    reference_result = compute_diffraction_reference(OpticalConfig())
    symmetry_result = compute_symmetry_measurement(stacked, profile_result, reference_result)
    assert symmetry_result.status is SymmetryStatus.LOW_CONFIDENCE

    result = FineCollimationResult(
        stack_result=StackResult(stacked=stacked, frame_count=4, quality_scores=(1.0,) * 4),
        profile_result=profile_result,
        reference_result=reference_result,
        symmetry_result=symmetry_result,
    )
    return FineCollimationOutcome(status="success", result=result, reason=None)


class TestAC7_1ValidResult:
    def test_a_valid_fine_collimated_result_shows_everything_ac_requires(
        self, qapp: object
    ) -> None:
        panel = _make_panel()
        outcome = _valid_fine_collimated_outcome()

        panel.show_outcome(outcome)

        assert panel._star_view.has_image is True
        assert "confidence" in panel._status_label.text().lower()
        assert panel._profile_widget.has_profile is True
        assert panel.is_collimated_style_active is True

    def test_asymmetric_result_shows_direction_but_not_collimated_style(
        self, qapp: object
    ) -> None:
        panel = _make_panel()
        # Reuse the valid case's stack but force an ASYMMETRIC-shaped result
        # by directly building one via the coma fixture used in issue #20's
        # own tests, to keep this a realistic (not hand-typed) result.
        stacked = coma_pattern_image(
            _SHAPE, x=_CENTER_X, y=_CENTER_Y, peak=6000.0, core_sigma=2.5, background=100.0,
            ring_radius_px=12.0, ring_peak=300.0, ring_sigma=1.5,
            asymmetry_direction_deg=45.0, asymmetry_strength=0.6,
        )
        profile_result = compute_radial_profile(stacked)
        reference_result = compute_diffraction_reference(OpticalConfig())
        symmetry_result = compute_symmetry_measurement(stacked, profile_result, reference_result)
        assert symmetry_result.status is SymmetryStatus.ASYMMETRIC

        outcome = FineCollimationOutcome(
            status="success",
            result=FineCollimationResult(
                stack_result=StackResult(stacked=stacked, frame_count=4, quality_scores=(1.0,) * 4),
                profile_result=profile_result,
                reference_result=reference_result,
                symmetry_result=symmetry_result,
            ),
            reason=None,
        )

        panel.show_outcome(outcome)

        assert panel.is_collimated_style_active is False
        assert panel._star_view.has_image is True


class TestAC7_2InvalidResult:
    def test_insufficient_sampling_shows_why_and_never_shows_collimated(
        self, qapp: object
    ) -> None:
        panel = _make_panel()
        outcome = _insufficient_sampling_outcome()

        panel.show_outcome(outcome)

        assert panel.is_collimated_style_active is False
        status_text = panel._status_label.text().lower()
        assert "insufficient" in status_text or "sampling" in status_text

    def test_low_confidence_shows_why_and_never_shows_collimated(self, qapp: object) -> None:
        panel = _make_panel()
        outcome = _low_confidence_outcome()

        panel.show_outcome(outcome)

        assert panel.is_collimated_style_active is False
        assert "confidence" in panel._status_label.text().lower()

    def test_a_failed_run_shows_the_reason_and_never_shows_collimated(
        self, qapp: object
    ) -> None:
        panel = _make_panel()
        outcome = FineCollimationOutcome(status="failed", result=None, reason="star_lost")

        panel.show_outcome(outcome)

        assert panel.is_collimated_style_active is False
        assert "star_lost" in panel._status_label.text()


class TestInitialState:
    def test_before_any_run_shows_a_neutral_not_yet_measured_state(self, qapp: object) -> None:
        panel = _make_panel()

        assert panel.is_collimated_style_active is False
        assert panel._star_view.has_image is False


class TestRunButtonNoFrameAvailable:
    def test_clicking_run_with_no_frame_available_reports_it_and_does_not_start(
        self, qapp: object
    ) -> None:
        panel = FineCollimationPanel(get_frame=lambda: None)

        panel._on_run_clicked()

        assert not panel._runner.is_busy
        assert "frame" in panel._status_label.text().lower()
