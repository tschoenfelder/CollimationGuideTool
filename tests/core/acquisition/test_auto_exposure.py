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

    Only the top 3 of the 100 pixels sit at that exact value; the rest
    are 0 — deliberately *not* mostly-uniform at that value, since
    auto_exposure's saturation-fraction check treats "most pixels sit at
    their own max" as a signal of genuine hardware saturation (see its
    docstring), and a normal in-range/dim/bright test frame must not
    trip that. The top 3 (of 100) is still enough for the 99th
    percentile to land exactly on the intended value — verified: with
    virtual index 98.01, both index 98 and 99 fall inside the top block."""
    value = fraction * _ADU_MAX
    frame = np.zeros(100, dtype=np.float32)
    frame[-3:] = value
    return frame.reshape(10, 10)


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


class TestSettlesInsteadOfOscillatingNearSaturation:
    """Follow-up real-hardware bug: the first saturation fix (a hard
    "fraction >= 50% saturated -> metric=1.0, else fall through to the
    naive percentile" cutoff) had a cliff at that 50% line. As
    corrections nudged the guide camera's clipped-pixel fraction back
    and forth across it, the reported metric jumped between ~1.0 and
    ~0.0625 every other frame — confirmed on the real GPCMOS02000KPA,
    oscillating indefinitely between ~1.5ms and ~4.5ms exposure instead
    of settling. _measure's max(naive, saturated_fraction) floor (no
    cutoff) fixes this by making the metric rise smoothly with the
    clipped fraction."""

    _CEILING = 4094.0  # the sensor's true ADC ceiling, well below bit_depth=16

    def _pixels_for_exposure(self, exposure_ms: float) -> np.ndarray:
        # A smooth per-pixel gradient up to 3x the true ceiling, scaled by
        # exposure and clipped at it — mimics a real partially-overexposed
        # scene where the clipped fraction grows continuously with
        # exposure, not a frame that is either fully flat or fully varied.
        gradient = np.linspace(0.0, self._CEILING * 3.0, 200 * 200).reshape(200, 200)
        raw = gradient * (exposure_ms / 5.0)
        return np.minimum(raw, self._CEILING).astype(np.float32)

    def test_exposure_settles_near_the_target_band_instead_of_oscillating(self) -> None:
        config = AutoExposureConfig()
        exposure_ms = 1.0
        gain = 100
        seen_settled = False
        for _ in range(40):
            pixels = self._pixels_for_exposure(exposure_ms)
            result = compute_auto_exposure(
                pixels,
                bit_depth=_BIT_DEPTH,
                current_exposure_ms=exposure_ms,
                current_gain=gain,
                capabilities=_CAPS,
                config=config,
            )
            exposure_ms, gain = result.exposure_ms, result.gain
            if not result.changed:
                seen_settled = True
                break
        assert seen_settled, "auto-exposure never reached a fixed point (still oscillating)"
        # "Close to 70%": the upper edge of the default target band, not
        # pinned to either saturation (1.0) or the pre-fix dim misread.
        assert config.target_low <= result.metric <= config.target_high

        # Once settled, it must actually stay settled — not just pause for
        # one frame before flipping again.
        for _ in range(5):
            pixels = self._pixels_for_exposure(exposure_ms)
            result = compute_auto_exposure(
                pixels,
                bit_depth=_BIT_DEPTH,
                current_exposure_ms=exposure_ms,
                current_gain=gain,
                capabilities=_CAPS,
                config=config,
            )
            assert result.changed is False
            assert config.target_low <= result.metric <= config.target_high


class TestAdaptiveGainStep:
    """Real request: a fixed +/-10 gain step either crawls or overshoots
    depending on how much a given camera's gain actually moves the
    metric. compute_auto_exposure now estimates that sensitivity from the
    caller-supplied previous (metric, gain) pair and solves directly for
    the step needed to reach target_mid, instead of always guessing
    +/-gain_step."""

    def test_with_no_previous_pair_falls_back_to_the_fixed_step(self) -> None:
        # Same as this file's existing fixed-step tests, but explicit
        # about *why*: previous_metric/previous_gain default to None.
        result = compute_auto_exposure(
            _frame(0.10),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=AutoExposureConfig().max_auto_exposure_ms,
            current_gain=100,
            capabilities=_CAPS,
        )
        assert result.gain == 110

    def test_second_correction_uses_the_estimated_sensitivity_not_the_fixed_step(self) -> None:
        config = AutoExposureConfig()
        # Simulates: the previous correction measured metric=0.10 at
        # gain=100 and stepped gain to 110 (the fixed-step fallback);
        # this frame, taken at that new gain, measures 0.20 -- i.e. +10
        # gain bought +0.10 metric, a sensitivity of 0.01/unit.
        result = compute_auto_exposure(
            _frame(0.20),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=config.max_auto_exposure_ms,
            current_gain=110,
            capabilities=_CAPS,
            config=config,
            previous_metric=0.10,
            previous_gain=100,
        )
        # needed = (target_mid=0.60 - metric=0.20) / sensitivity=0.01 = 40
        assert result.gain == 150
        assert result.changed is True

    def test_a_large_estimated_step_is_clamped_to_max_gain_step(self) -> None:
        config = AutoExposureConfig()
        # A tiny sensitivity (a large gain change barely moved the metric)
        # would otherwise demand a very large next step.
        result = compute_auto_exposure(
            _frame(0.20),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=config.max_auto_exposure_ms,
            current_gain=110,
            capabilities=_CAPS,
            config=config,
            previous_metric=0.19,
            previous_gain=100,
        )
        # sensitivity = (0.20-0.19)/(110-100) = 0.001/unit; naive needed =
        # (0.60-0.20)/0.001 = 400 -- clamped to the default max_gain_step (50).
        assert result.gain == 110 + config.max_gain_step

    def test_falls_back_to_the_fixed_step_when_the_previous_gain_did_not_actually_change(
        self,
    ) -> None:
        # The previous correction's gain equals this one's current_gain
        # (e.g. it was exposure-only) -- no gain delta to estimate a
        # slope from, so this must not divide by ~zero.
        config = AutoExposureConfig()
        result = compute_auto_exposure(
            _frame(0.20),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=config.max_auto_exposure_ms,
            current_gain=110,
            capabilities=_CAPS,
            config=config,
            previous_metric=0.15,
            previous_gain=110,
        )
        assert result.gain == 120  # 110 + the fixed gain_step (10)

    def test_too_bright_direction_also_uses_the_estimated_sensitivity(self) -> None:
        config = AutoExposureConfig()
        # The previous correction dropped gain 210 -> 200 (delta -10) and
        # metric fell 0.95 -> 0.90 (this frame) -- sensitivity 0.005/unit,
        # same magnitude-clamping and sign-solving as the too-dim case.
        result = compute_auto_exposure(
            _frame(0.90),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=_CAPS.min_exposure_ms,
            current_gain=200,
            capabilities=_CAPS,
            config=config,
            previous_metric=0.95,
            previous_gain=210,
        )
        # needed = (0.60-0.90)/0.005 = -60 -- clamped to -max_gain_step (50).
        assert result.gain == 200 - config.max_gain_step
        assert result.changed is True


class TestGainUnwind:
    """Real report (diagnostic fa1167b4): gain could be left stuck high
    from an earlier correction (e.g. exposure was once pinned at the
    ceiling) even after exposure alone has since come back down to a
    normal value handling a brighter scene -- the metric reads fine, but
    the actual working point (very short exposure, very high gain) is
    mostly amplified read noise. compute_auto_exposure now walks gain
    back toward default_gain whenever a frame is already in-band and
    exposure has room to compensate.

    The unwind step itself halves gain (bounded by max_step_factor, same
    clamp exposure's escalation already uses), not a fixed gain_step --
    real follow-up report: a fixed step of 10 took over a thousand
    corrections to walk a gain elevated by 1000+ back down."""

    def test_in_band_with_elevated_gain_halves_and_scales_exposure_up(self) -> None:
        result = compute_auto_exposure(
            _frame(0.6),  # squarely in the default [0.50, 0.70] band
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=100.0,
            current_gain=800,
            capabilities=_CAPS,
        )
        assert result.changed is True
        assert result.gain == 400  # halved, default max_step_factor=2.0
        # Same linear signal-vs-exposure/gain model the escalation path
        # already uses: exposure scales up by the same ratio gain scaled
        # down, to preserve the same measured signal.
        assert result.exposure_ms == 100.0 * (800 / 400)

    def test_in_band_at_default_gain_does_not_unwind(self) -> None:
        result = compute_auto_exposure(
            _frame(0.6),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=1000.0,
            current_gain=AutoExposureConfig().default_gain,
            capabilities=_CAPS,
        )
        assert result.changed is False
        assert result.gain == AutoExposureConfig().default_gain
        assert result.exposure_ms == 1000.0

    def test_unwind_is_skipped_when_the_compensated_exposure_would_exceed_the_ceiling(
        self,
    ) -> None:
        config = AutoExposureConfig()
        # Already close to the live-view ceiling -- scaling exposure up
        # by 200/190 would push it past 2000ms.
        result = compute_auto_exposure(
            _frame(0.6),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=config.max_auto_exposure_ms - 1.0,
            current_gain=200,
            capabilities=_CAPS,
            config=config,
        )
        assert result.changed is False
        assert result.gain == 200
        assert result.exposure_ms == config.max_auto_exposure_ms - 1.0

    def test_a_zero_gain_step_never_overshoots_below_default_gain(self) -> None:
        # Only 5 gain above default -- the fixed gain_step (10) would
        # overshoot past it if not clamped to the remaining gap.
        result = compute_auto_exposure(
            _frame(0.6),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=100.0,
            current_gain=AutoExposureConfig().default_gain + 5,
            capabilities=_CAPS,
        )
        assert result.gain == AutoExposureConfig().default_gain

    def test_repeated_in_band_corrections_eventually_settle_at_default_gain(self) -> None:
        # Simulates several polled frames in a row, all reading in-band --
        # gain should walk itself all the way back down over time, not
        # just once.
        exposure_ms = 50.0
        gain = 200
        config = AutoExposureConfig()
        for _ in range(20):
            result = compute_auto_exposure(
                _frame(0.6),
                bit_depth=_BIT_DEPTH,
                current_exposure_ms=exposure_ms,
                current_gain=gain,
                capabilities=_CAPS,
                config=config,
            )
            exposure_ms, gain = result.exposure_ms, result.gain
            if not result.changed:
                break
        assert gain == config.default_gain

    def test_a_very_elevated_gain_converges_in_a_handful_of_corrections_not_thousands(
        self,
    ) -> None:
        # Real follow-up report: "still reduced gain in steps of 10,
        # moving down by >1000 only slowly" -- the fixed-step version of
        # this would need over three hundred corrections to walk a gain
        # this elevated back down to the default (100); halving needs a
        # handful.
        exposure_ms = 2.0
        gain = _CAPS.max_gain  # 3200 -- as elevated as this camera allows
        config = AutoExposureConfig()
        corrections = 0
        for _ in range(20):
            result = compute_auto_exposure(
                _frame(0.6),
                bit_depth=_BIT_DEPTH,
                current_exposure_ms=exposure_ms,
                current_gain=gain,
                capabilities=_CAPS,
                config=config,
            )
            if not result.changed:
                break
            exposure_ms, gain = result.exposure_ms, result.gain
            corrections += 1
        assert gain == config.default_gain
        assert corrections < 20

    def test_an_out_of_band_frame_still_gets_its_normal_correction_regardless_of_gain(
        self,
    ) -> None:
        # Unwinding must never pre-empt the normal too-dim/too-bright
        # correction path -- this frame is genuinely too dim and needs
        # *more* exposure, not a gain unwind.
        result = compute_auto_exposure(
            _frame(0.10),
            bit_depth=_BIT_DEPTH,
            current_exposure_ms=1000.0,
            current_gain=200,
            capabilities=_CAPS,
        )
        assert result.exposure_ms > 1000.0
        assert result.gain == 200  # untouched -- exposure has headroom


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
