import pytest
from collimation_tool.domain.focus_metric import classify_focus_quality, mean_fwhm_px


def test_excellent_tier() -> None:
    quality = classify_focus_quality(1.5)
    assert quality.tier == "excellent"
    assert quality.is_in_focus is True


def test_good_tier() -> None:
    quality = classify_focus_quality(3.0)
    assert quality.tier == "good"
    assert quality.is_in_focus is True


def test_poor_tier() -> None:
    quality = classify_focus_quality(6.0)
    assert quality.tier == "poor"
    assert quality.is_in_focus is False


def test_boundary_values_are_inclusive() -> None:
    assert classify_focus_quality(2.0).tier == "excellent"
    assert classify_focus_quality(4.0).tier == "good"


def test_custom_thresholds() -> None:
    quality = classify_focus_quality(3.0, excellent_fwhm_px=5.0, good_fwhm_px=10.0)
    assert quality.tier == "excellent"


def test_mean_fwhm_px() -> None:
    assert mean_fwhm_px(2.0, 4.0) == pytest.approx(3.0)
