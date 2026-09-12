"""Tests for FocusedStarAcquisition — issue #15 Stage 1, one test group
per acceptance criterion (AC 1.1-1.7)."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.registration.geometry import polygon_centroid, rect_polygon
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import (
    CrossCameraRegistrationResult,
    RegistrationMethod,
    RegistrationStatus,
)
from astrotool_core.testing.frame_factory import StarSpec, single_star_image, star_field_image
from collimation_tool.application.star_acquisition import AcquisitionStatus, FocusedStarAcquisition

_MAIN_SHAPE = (80, 100)  # height, width
_GUIDE_SHAPE = (300, 400)

_RESPONSE_VECTORS = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): (10.0, 0.0),
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): (-10.0, 0.0),
    (MountAxis.AXIS2, AxisDirection.POSITIVE): (0.0, 10.0),
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): (0.0, -10.0),
}


def _make_calibration(px_per_ms: float = 0.1, duration_ms: int = 100) -> CalibrationMatrix:
    responses = {
        (axis, direction): AxisResponse(
            axis=axis, direction=direction, duration_ms=duration_ms,
            dx_px=dx, dy_px=dy, px_per_ms=px_per_ms,
        )
        for (axis, direction), (dx, dy) in _RESPONSE_VECTORS.items()
    }
    return CalibrationMatrix(responses=responses)


class _MovingGuideMount:
    """Test double: applies each accepted pulse to a simulated guide-space
    star position and can render a fresh synthetic guide frame reflecting
    it -- extends `test_recenter_policy.py`'s own `MovingMount` pattern
    with actual frame rendering, since this controller's own `measure`
    closure re-detects from real pixels rather than taking a
    hand-built `TrackingResult` directly."""

    def __init__(
        self, calibration: CalibrationMatrix, start: tuple[float, float], *, accept: bool = True
    ) -> None:
        self._calibration = calibration
        self.x, self.y = start
        self.pulse_log: list[tuple[MountAxis, AxisDirection, int]] = []
        self._accept = accept

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9999)

    def status(self) -> MountStatus:
        return MountStatus(connected=True, tracking=True, slewing=False)

    def pulse_axis(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int,
        *, rate_preset: str | None = None,
    ) -> CommandResult:
        self.pulse_log.append((axis, direction, duration_ms))
        if not self._accept:
            return CommandResult(accepted=False, message="rejected for test")
        response = self._calibration.response_for(axis, direction)
        per_ms_x = response.dx_px / response.duration_ms
        per_ms_y = response.dy_px / response.duration_ms
        self.x += per_ms_x * duration_ms
        self.y += per_ms_y * duration_ms
        return CommandResult(accepted=True)

    def render_frame(self) -> np.ndarray:
        return single_star_image(
            _GUIDE_SHAPE, x=self.x, y=self.y, peak=3000.0, sigma=2.0, background=100.0
        )


def _empty_main_frame() -> np.ndarray:
    return star_field_image(_MAIN_SHAPE, [], background=50.0)


def _main_star(x: float, y: float, *, peak: float = 3000.0, sigma: float = 2.0) -> np.ndarray:
    return single_star_image(_MAIN_SHAPE, x=x, y=y, peak=peak, sigma=sigma, background=100.0)


class TestAC1_2FollowStarInsideRoi:
    def test_a_small_known_offset_keeps_the_same_star_and_moves_the_roi(self) -> None:
        controller = FocusedStarAcquisition(roi_size=(24, 20))
        frame1 = _main_star(40.0, 30.0)
        first = controller.select(frame1)
        assert first.status is AcquisitionStatus.TRACKING

        frame2 = _main_star(43.0, 32.0)
        result = controller.process_main_frame(frame2)

        assert result.status is AcquisitionStatus.TRACKING
        assert result.target_position is not None
        assert result.target_position == pytest.approx((43.0, 32.0), abs=1.0)
        assert result.roi is not None


class TestAC1_3StarLeavesRoiButStaysInFrame:
    def test_reacquired_in_the_full_frame_moves_roi_never_reports_lost(self) -> None:
        controller = FocusedStarAcquisition(
            roi_size=(16, 12), max_position_error_px=100.0
        )
        frame1 = _main_star(20.0, 20.0)
        controller.select(frame1)

        # Moved well outside a 16x12 ROI (half-size 8x6) but still
        # comfortably inside the main frame and within the configured
        # full-frame search tolerance.
        frame2 = _main_star(60.0, 55.0)
        result = controller.process_main_frame(frame2)

        assert result.status is AcquisitionStatus.TRACKING
        assert result.roi is not None
        assert result.target_position == pytest.approx((60.0, 55.0), abs=1.0)

    def test_repeated_full_frame_misses_eventually_report_lost(self) -> None:
        controller = FocusedStarAcquisition(
            roi_size=(16, 12), max_full_frame_search_attempts=3
        )
        frame1 = _main_star(20.0, 20.0)
        controller.select(frame1)

        empty = _empty_main_frame()
        r1 = controller.process_main_frame(empty)
        r2 = controller.process_main_frame(empty)
        r3 = controller.process_main_frame(empty)

        assert r1.status is AcquisitionStatus.SEARCHING_MAIN
        assert r2.status is AcquisitionStatus.SEARCHING_MAIN
        assert r3.status is AcquisitionStatus.LOST
        assert r3.failure_reason == "target_not_found_main"


def _guide_registration(
    confidence: float = 0.9,
) -> tuple[OpticalPrior, CrossCameraRegistrationResult]:
    prior_main = OpticalPrior(
        name="main", sensor_width_px=_MAIN_SHAPE[1], sensor_height_px=_MAIN_SHAPE[0],
        pixel_scale_arcsec=1.0,
    )
    scale = 0.25
    polygon = rect_polygon(
        _MAIN_SHAPE[1] * scale, _MAIN_SHAPE[0] * scale, center=(200.0, 150.0), rotation_deg=0.0,
    )
    registration = CrossCameraRegistrationResult(
        method=RegistrationMethod.TERRESTRIAL, status=RegistrationStatus.OK_OVERLAP,
        rotation_deg=0.0, scale=scale, polygon_a_in_b=polygon, confidence=confidence,
    )
    return prior_main, registration


class TestAC1_4GuideAssistedReacquisition:
    def test_identifies_the_star_in_guide_and_recenters_toward_main_fov(self) -> None:
        prior_main, registration = _guide_registration()
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        # Star near main's own right edge (90, 40), about to leave.
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        calibration = _make_calibration()
        # Predicted guide position for (90,40) is (210, 150) -- start the
        # simulated guide star further off, so real pulses are needed.
        mount = _MovingGuideMount(calibration, start=(260.0, 150.0))

        result = controller.attempt_guide_reacquisition(
            mount.render_frame, mount=mount, guide_calibration=calibration,
            registration=registration, prior_main=prior_main,
        )

        assert result.status is AcquisitionStatus.SEARCHING_GUIDE
        assert result.failure_reason is None
        assert len(mount.pulse_log) > 0
        assert registration.polygon_a_in_b is not None
        target_center = polygon_centroid(registration.polygon_a_in_b)
        assert mount.x == pytest.approx(target_center[0], abs=5.0)
        assert mount.y == pytest.approx(target_center[1], abs=5.0)

    def test_refuses_automatic_movement_below_the_confidence_threshold(self) -> None:
        prior_main, registration = _guide_registration(confidence=0.1)
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        calibration = _make_calibration()
        mount = _MovingGuideMount(calibration, start=(210.0, 150.0))

        result = controller.attempt_guide_reacquisition(
            mount.render_frame, mount=mount, guide_calibration=calibration,
            registration=registration, prior_main=prior_main,
        )

        assert result.failure_reason == "insufficient_calibration_confidence"
        assert mount.pulse_log == []

    def test_not_found_in_guide_reports_target_not_found_guide(self) -> None:
        prior_main, registration = _guide_registration()
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        calibration = _make_calibration()
        mount = _MovingGuideMount(calibration, start=(210.0, 150.0))

        def empty_guide_frame() -> np.ndarray:
            return star_field_image(_GUIDE_SHAPE, [], background=50.0)

        result = controller.attempt_guide_reacquisition(
            empty_guide_frame, mount=mount, guide_calibration=calibration,
            registration=registration, prior_main=prior_main,
        )

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == "target_not_found_guide"

    def test_mount_rejection_is_reported_distinctly(self) -> None:
        prior_main, registration = _guide_registration()
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        calibration = _make_calibration()
        mount = _MovingGuideMount(calibration, start=(260.0, 150.0), accept=False)

        result = controller.attempt_guide_reacquisition(
            mount.render_frame, mount=mount, guide_calibration=calibration,
            registration=registration, prior_main=prior_main,
        )

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == "mount_correction_rejected"

    def test_cancellation_is_reported_distinctly(self) -> None:
        prior_main, registration = _guide_registration()
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        calibration = _make_calibration()
        mount = _MovingGuideMount(calibration, start=(260.0, 150.0))

        result = controller.attempt_guide_reacquisition(
            mount.render_frame, mount=mount, guide_calibration=calibration,
            registration=registration, prior_main=prior_main,
            cancel_check=lambda: True,
        )

        assert result.status is AcquisitionStatus.CANCELLED
        assert result.failure_reason == "cancelled"

    def test_diverging_correction_is_reported_distinctly(self) -> None:
        prior_main, registration = _guide_registration()
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        calibration = _make_calibration()
        positions = iter([260.0, 270.0, 285.0, 305.0, 330.0, 360.0])

        class _DivergingMount(_MovingGuideMount):
            def pulse_axis(
                self, axis: MountAxis, direction: AxisDirection, duration_ms: int,
                *, rate_preset: str | None = None,
            ) -> CommandResult:
                self.pulse_log.append((axis, direction, duration_ms))
                self.x = next(positions)
                return CommandResult(accepted=True)

        mount = _DivergingMount(calibration, start=(260.0, 150.0))

        result = controller.attempt_guide_reacquisition(
            mount.render_frame, mount=mount, guide_calibration=calibration,
            registration=registration, prior_main=prior_main,
        )

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == "reacquisition_diverging"


class TestAC1_5TransferBackToMainTracking:
    def test_confirming_the_star_in_a_fresh_main_frame_resumes_tracking(self) -> None:
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        # The star has reappeared near the main frame's own center after
        # guide-driven recentering -- no new manual selection performed.
        fresh_main = single_star_image(
            _MAIN_SHAPE, x=50.0, y=40.0, peak=3000.0, sigma=2.0, background=100.0
        )
        result = controller.confirm_returned_to_main(fresh_main)

        assert result.status is AcquisitionStatus.TRACKING
        assert result.roi is not None
        assert result.target_position == pytest.approx((50.0, 40.0), abs=1.0)

    def test_not_found_after_guide_convergence_reports_timeout_not_infinite_retry(self) -> None:
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        result = controller.confirm_returned_to_main(_empty_main_frame())

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == "reacquisition_timeout"


class TestAC1_6PreserveTargetIdentity:
    def test_multiple_similarly_placed_and_bright_candidates_in_main_are_ambiguous(self) -> None:
        controller = FocusedStarAcquisition(roi_size=(16, 12), max_position_error_px=30.0)
        frame1 = _main_star(40.0, 30.0)
        controller.select(frame1)

        ambiguous_frame = star_field_image(
            _MAIN_SHAPE,
            [
                StarSpec(x=48.0, y=30.0, peak=3000.0, sigma=2.0),
                StarSpec(x=32.0, y=30.0, peak=3000.0, sigma=2.0),
            ],
            background=100.0,
        )
        result = controller.process_main_frame(ambiguous_frame)

        assert result.status is AcquisitionStatus.AMBIGUOUS
        assert result.failure_reason == "target_ambiguous"

    def test_multiple_similarly_placed_and_bright_candidates_in_guide_are_ambiguous(self) -> None:
        prior_main, registration = _guide_registration()
        controller = FocusedStarAcquisition(roi_size=(16, 12))
        frame1 = _main_star(90.0, 40.0)
        controller.select(frame1)

        calibration = _make_calibration()
        mount = _MovingGuideMount(calibration, start=(210.0, 150.0))

        def ambiguous_guide_frame() -> np.ndarray:
            # Spaced far enough apart (30px, sigma=2.0) to remain two
            # distinct detections rather than merging into one blob --
            # equidistant (15px each) from the predicted (210, 150).
            return star_field_image(
                _GUIDE_SHAPE,
                [
                    StarSpec(x=225.0, y=150.0, peak=3000.0, sigma=2.0),
                    StarSpec(x=195.0, y=150.0, peak=3000.0, sigma=2.0),
                ],
                background=100.0,
            )

        result = controller.attempt_guide_reacquisition(
            ambiguous_guide_frame, mount=mount, guide_calibration=calibration,
            registration=registration, prior_main=prior_main,
        )

        assert result.status is AcquisitionStatus.AMBIGUOUS
        assert result.failure_reason == "target_ambiguous"
        assert mount.pulse_log == []


class TestAC1_7ReacquisitionFailure:
    def test_no_star_anywhere_in_main_after_retries_reports_lost_and_stops(self) -> None:
        controller = FocusedStarAcquisition(
            roi_size=(16, 12), max_full_frame_search_attempts=2
        )
        frame1 = _main_star(20.0, 20.0)
        controller.select(frame1)

        empty = _empty_main_frame()
        controller.process_main_frame(empty)
        result = controller.process_main_frame(empty)

        assert result.status is AcquisitionStatus.LOST
        assert result.failure_reason == "target_not_found_main"
        assert result.roi is None
