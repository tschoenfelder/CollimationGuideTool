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
    """A frame whose 99th percentile is exactly `fraction` of full ADU range.

    99 of the 100 pixels sit at that exact value and one is dropped to 0 —
    not perfectly uniform, matching the fact that real sensor data always
    carries some read noise (see auto_exposure's saturation-fraction
    check, which treats a *perfectly flat* frame as a signal of genuine
    hardware saturation, not something a normal in-range/dim/bright frame
    should ever trigger). The single low outlier never lands in the top
    99th-percentile band, so it doesn't change any test's expected metric."""
    value = fraction * _ADU_MAX
    frame = np.full((10, 10), value, dtype=np.float32)
    frame.flat[0] = 0.0
    return frame


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


def test_too_dim_at_camera_hardware_max_raises_gain_instead() -> None:
    # max_auto_exposure_ms raised out of the way so this specifically
    # exercises the camera's own hardware ceiling, not the live-view one.
    config = AutoExposureConfig(max_auto_exposure_ms=_CAPS.max_exposure_ms)
    result = compute_auto_exposure(
        _frame(0.10),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=_CAPS.max_exposure_ms,
        current_gain=100,
        capabilities=_CAPS,
        config=config,
    )
    assert result.exposure_ms == _CAPS.max_exposure_ms  # unchanged — already maxed
    assert result.gain == 110  # default gain_step=10
    assert result.changed is True


class TestLiveViewExposureCeiling:
    """See the real-hardware bug this was found from: auto-exposure could
    climb from a tiny exposure past 10 real seconds within a few
    wall-clock seconds (each doubling step reacts to whatever frame just
    arrived, and frames arrive fast at short exposures) — then the camera
    blocked every capture for that full duration, freezing the "live"
    view. max_auto_exposure_ms caps the climb well below a camera's own
    (often huge) hardware max_exposure_ms."""

    def test_default_ceiling_is_2_seconds(self) -> None:
        assert AutoExposureConfig().max_auto_exposure_ms == 2000.0

    def test_too_dim_at_the_default_ceiling_raises_gain_not_exposure(self) -> None:
        # 2000ms is nowhere near _CAPS.max_exposure_ms (60_000ms) — proves
        # the *live-view* ceiling is what's actually enforced by default,
        # not the camera's hardware limit.
        config = AutoExposureConfig()
        result = compute_auto_exposure(
            _frame(0.10),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=config.max_auto_exposure_ms,
            current_gain=100,
            capabilities=_CAPS,
            config=config,
        )
        assert result.exposure_ms == config.max_auto_exposure_ms
        assert result.gain == 110
        assert result.changed is True

    def test_exposure_never_exceeds_the_ceiling_even_from_a_tiny_starting_point(
        self,
    ) -> None:
        # Simulates the reported runaway: start at the camera's minimum
        # and let a very dim frame drive repeated corrections.
        exposure_ms = _CAPS.min_exposure_ms
        gain = 100
        config = AutoExposureConfig()
        for _ in range(30):
            result = compute_auto_exposure(
                _frame(0.001),
                bit_depth=_BIT_DEPTH,
                current_exposure_ms=exposure_ms,
                current_gain=gain,
                capabilities=_CAPS,
                config=config,
            )
            exposure_ms, gain = result.exposure_ms, result.gain
            assert exposure_ms <= config.max_auto_exposure_ms
        # It should actually have reached the ceiling, not stalled short of it.
        assert exposure_ms == config.max_auto_exposure_ms

    def test_a_camera_with_a_lower_hardware_max_than_the_ceiling_still_wins(self) -> None:
        # The smaller of the two limits always applies.
        low_max_caps = CameraCapabilities(
            min_gain=100,
            max_gain=3200,
            min_exposure_ms=0.1,
            max_exposure_ms=500.0,  # below the 2000ms default ceiling
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
        result = compute_auto_exposure(
            _frame(0.001),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=500.0,
            current_gain=100,
            capabilities=low_max_caps,
        )
        assert result.exposure_ms == 500.0
        assert result.gain == 110


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


def test_fully_black_frame_pushes_exposure_toward_the_live_view_ceiling() -> None:
    result = compute_auto_exposure(
        np.zeros((10, 10), dtype=np.float32),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
    )
    # Not _CAPS.max_exposure_ms (60_000ms, the camera's hardware limit) —
    # the live-view ceiling (2000ms default) is lower and wins.
    assert result.exposure_ms == AutoExposureConfig().max_auto_exposure_ms
    assert result.gain == 100  # exposure hasn't been exhausted yet this step
    assert result.metric == 0.0


def test_fully_black_frame_with_the_ceiling_raised_out_of_the_way_reaches_camera_max() -> None:
    config = AutoExposureConfig(max_auto_exposure_ms=_CAPS.max_exposure_ms)
    result = compute_auto_exposure(
        np.zeros((10, 10), dtype=np.float32),
        bit_depth=_BIT_DEPTH,
        current_exposure_ms=1000.0,
        current_gain=100,
        capabilities=_CAPS,
        config=config,
    )
    assert result.exposure_ms == _CAPS.max_exposure_ms


def test_default_gain_is_100_matching_every_supported_camera_minimum() -> None:
    assert AutoExposureConfig().default_gain == 100


class TestSaturationAtALowerTrueAdcCeiling:
    """Real-hardware bug ("guide stays black"): a sensor whose true ADC
    ceiling is well below the assumed bit_depth (e.g. ~4094 on a camera
    reporting bit_depth=16, because pixel-shift detection can only
    recognize specific MSB-padding patterns, not learn a sensor's actual
    ADC width) can sit fully, perfectly saturated at that real ceiling —
    confirmed on real hardware: raising gain 100->5000 left the frame
    completely unchanged. The naive signal/(2**bit_depth-1) computation
    then reads this as only a few percent "bright", so auto-exposure
    concludes "far too dim" and escalates exposure/gain without bound,
    chasing a target the sensor can never produce."""

    def test_a_perfectly_flat_frame_pinned_well_below_assumed_max_reads_as_fully_saturated(
        self,
    ) -> None:
        # A real, perfectly uniform ~4094 frame, exactly the observed bug —
        # naively 4094 / 65535 = 6.25%, i.e. "far too dim". Correctly read
        # as fully saturated (metric=1.0), it's treated the same as any
        # too-bright frame: exposure is backed off, not escalated further —
        # the fix for a real runaway where gain climbed 100->380+ while
        # the sensor stayed pinned at its true, lower ADC ceiling the
        # whole time, never actually getting brighter.
        pinned = np.full((10, 10), 4094.0, dtype=np.float32)
        result = compute_auto_exposure(
            pinned,
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=1000.0,
            current_gain=380,  # already escalated once, per the real report
            capabilities=_CAPS,
        )
        assert result.metric == 1.0
        assert result.changed is True
        assert result.exposure_ms < 1000.0  # backs off instead of escalating
        assert result.gain == 380  # gain untouched — exposure has headroom


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
