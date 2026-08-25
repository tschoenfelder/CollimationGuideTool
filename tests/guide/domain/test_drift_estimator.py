import pytest
from guide_tool.domain.drift_estimator import DriftEstimator


def test_rms_is_zero_with_fewer_than_two_samples() -> None:
    estimator = DriftEstimator()
    assert estimator.rms_px() == 0.0
    estimator.add(5.0)
    assert estimator.rms_px() == 0.0


def test_rms_of_constant_error_equals_that_error() -> None:
    estimator = DriftEstimator()
    for _ in range(5):
        estimator.add(2.0)
    assert estimator.rms_px() == pytest.approx(2.0)


def test_window_drops_oldest_samples() -> None:
    estimator = DriftEstimator(window=3)
    for value in (100.0, 100.0, 100.0, 0.0, 0.0, 0.0):
        estimator.add(value)
    # only the three most-recent zeros remain in the window
    assert estimator.rms_px() == 0.0


def test_reset_clears_history() -> None:
    estimator = DriftEstimator()
    estimator.add(5.0)
    estimator.add(5.0)
    estimator.reset()
    assert estimator.rms_px() == 0.0
