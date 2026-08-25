import pytest
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState
from guide_tool.domain.guide_error import compute_guide_error


def _result(state: TrackingState, x: float | None, y: float | None) -> TrackingResult:
    return TrackingResult(state=state, x=x, y=y, matched_source=None)


def test_locked_with_target_computes_error() -> None:
    result = _result(TrackingState.LOCKED, 105.0, 98.0)
    error = compute_guide_error(result, target=(100.0, 100.0))
    assert error.accepted is True
    assert error.error_x == pytest.approx(5.0)
    assert error.error_y == pytest.approx(-2.0)
    assert error.error_magnitude_px == pytest.approx((5.0**2 + 2.0**2) ** 0.5)


def test_reacquired_is_also_accepted() -> None:
    result = _result(TrackingState.REACQUIRED, 100.0, 100.0)
    error = compute_guide_error(result, target=(100.0, 100.0))
    assert error.accepted is True
    assert error.error_magnitude_px == pytest.approx(0.0)


def test_no_target_yet_reports_position_without_error() -> None:
    result = _result(TrackingState.LOCKED, 105.0, 98.0)
    error = compute_guide_error(result, target=None)
    assert error.accepted is True
    assert error.centroid_x == 105.0
    assert error.error_x is None
    assert error.error_magnitude_px is None


@pytest.mark.parametrize("state", [TrackingState.LOST, TrackingState.SEARCHING])
def test_lost_or_searching_is_rejected(state: TrackingState) -> None:
    result = _result(state, 100.0, 100.0)
    error = compute_guide_error(result, target=(100.0, 100.0))
    assert error.accepted is False
    assert error.rejected_reason == "star_lost"
