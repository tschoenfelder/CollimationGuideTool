import numpy as np
from astrotool_core.acquisition.auto_exposure import (
    AutoExposureConfig,
    compute_auto_exposure,
)
from astrotool_core.camera.capabilities import CameraCapabilities

_BIT_DEPTH = 16
_ADU_MAX = 2**_BIT_DEPTH - 1

_CAPS = CameraCapabilities(
    min_gain=100,
    max_gain=3200,
    min_exposure_ms=0.1,
    max_exposure_ms=60_000.0,
    supports_cooling=False,
    supports_hcg=False,
    supports_lcg=False,
    supports_hdr=False,
    supports_black_level=False,
    bit_depth=_BIT_DEPTH,
    pixel_size_um=2.4,
    sensor_width_px=64,
    sensor_height_px=64,
)


def _frame(fraction: float) -> np.ndarray:
    """A uniform frame whose 99th percentile is exactly `fraction` of full ADU range."""
    return np.full((10, 10), fraction * _ADU_MAX, dtype=np.float32)


def test_in_range_metric_leaves_settings_unchanged() -> None:
    result = compute_auto_exposure(
        _frame(0.6),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
    )
    assert result.changed is False
    assert result.exposure_ms == 1000.0
    assert result.gain == 100
    assert result.metric == 0.6


def test_boundary_values_count_as_in_range() -> None:
    low = compute_auto_exposure(
        _frame(0.50),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
    )
    high = compute_auto_exposure(
        _frame(0.70),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
    )
    assert low.changed is False
    assert high.changed is False


def test_too_dim_with_exposure_headroom_increases_exposure_not_gain() -> None:
    result = compute_auto_exposure(
        _frame(0.10),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
    )
    assert result.changed is True
    assert result.exposure_ms > 1000.0
    assert result.gain == 100


def test_exposure_step_is_clamped_to_max_step_factor() -> None:
    # target_mid / metric = 0.6 / 0.05 = 12x — must clamp to the 2x default step.
    result = compute_auto_exposure(
        _frame(0.05),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
    )
    assert result.exposure_ms == 2000.0
    assert result.gain == 100


def test_too_dim_at_max_exposure_raises_gain_instead() -> None:
    result = compute_auto_exposure(
        _frame(0.10),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=_CAPS.max_exposure_ms,
        current_gain=100,
        capabilities=_CAPS,
    )
    assert result.exposure_ms == _CAPS.max_exposure_ms  # unchanged — already maxed
    assert result.gain == 110  # default gain_step=10
    assert result.changed is True


def test_gain_never_exceeds_camera_max() -> None:
    result = compute_auto_exposure(
        _frame(0.0),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=_CAPS.max_exposure_ms,
        current_gain=_CAPS.max_gain,
        capabilities=_CAPS,
    )
    assert result.gain == _CAPS.max_gain


def test_too_bright_with_exposure_headroom_decreases_exposure_not_gain() -> None:
    result = compute_auto_exposure(
        _frame(0.90),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
    )
    assert result.changed is True
    assert result.exposure_ms < 1000.0
    assert result.gain == 100


def test_too_bright_at_min_exposure_lowers_gain_instead() -> None:
    result = compute_auto_exposure(
        _frame(0.90),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=_CAPS.min_exposure_ms,
        current_gain=150,
        capabilities=_CAPS,
    )
    assert result.exposure_ms == _CAPS.min_exposure_ms
    assert result.gain == 140  # default gain_step=10
    assert result.changed is True


def test_gain_never_drops_below_camera_min() -> None:
    result = compute_auto_exposure(
        _frame(1.0),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=_CAPS.min_exposure_ms,
        current_gain=_CAPS.min_gain,
        capabilities=_CAPS,
    )
    assert result.gain == _CAPS.min_gain


def test_fully_black_frame_pushes_exposure_toward_max() -> None:
    result = compute_auto_exposure(
        np.zeros((10, 10), dtype=np.float32),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
    )
    assert result.exposure_ms == _CAPS.max_exposure_ms
    assert result.gain == 100  # exposure hasn't been exhausted yet this step
    assert result.metric == 0.0


def test_default_gain_is_100_matching_every_supported_camera_minimum() -> None:
    assert AutoExposureConfig().default_gain == 100


def test_custom_target_band_is_respected() -> None:
    config = AutoExposureConfig(target_low=0.2, target_high=0.3)
    result = compute_auto_exposure(
        _frame(0.25),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
        config=config,
    )
    assert result.changed is False
