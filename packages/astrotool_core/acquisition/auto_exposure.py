"""Histogram-based auto exposure/gain for the live camera view.

Product decision: gain stays at its baseline (100 — the minimum gain on
every camera this project supports; see `fake_camera.py`/`replay_camera.py`/
`touptek_adapter.py`'s capabilities) unless exposure alone cannot bring the
frame's brightest real signal into the 50-70% ADU target band. Only then
does gain step up (or back down, for the symmetric too-bright case); it is
never touched while exposure alone is doing the job.

Deliberately a single stateless "compute one correction" function called
once per polled frame — the same call-once-per-frame shape as
`CollimationController.measure_and_advise` and `GuideCorrectionPolicy`, not
a closed PID loop. The caller (a UI) applies the returned exposure/gain to
the `CameraPort` and updates its controls; nothing here touches a camera
directly, so it needs no hardware/mocks to test.

Scope trim: this does not "unwind" gain back toward 100 once conditions
improve (e.g. a brighter object framed after gain was raised for a dimmer
one) — each call only reacts to the current frame being outside the target
band. Revisit if that turns out to matter in practice.

Live-view exposure ceiling (see `AutoExposureConfig.max_auto_exposure_ms`):
found via real-hardware testing that this algorithm could "run away" —
each doubling step reacts to whatever frame just arrived, and at short
exposures frames arrive almost as fast as the poll rate, so a dim scene
could climb from 0.1ms past 10 SECONDS within a few real wall-clock
seconds. The camera then blocks each capture for that full duration —
freezing the "live" view for 10+ real seconds per frame, which looks
indistinguishable from "the display stopped updating" or "exposure
changes aren't being applied" (they were; the camera was just busy
actually exposing). `max_auto_exposure_ms` caps the climb at a much
smaller, still-actually-live value, well below a camera's own hardware
maximum (e.g. the ATR585M's is 3,600,000ms — one hour) — beyond that
ceiling, gain takes over exactly as it already does at the camera's own
hardware limit.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astrotool_core.camera.capabilities import CameraCapabilities


@dataclass(frozen=True)
class AutoExposureConfig:
    target_low: float = 0.50  # fraction of full ADU range (2**bit_depth - 1)
    target_high: float = 0.70
    default_gain: int = 100
    #: Percentile of the frame's pixel distribution treated as "the signal"
    #: — not the mean, since astro frames are mostly near-black background.
    percentile: float = 99.0
    #: Clamp on how much exposure may change in one step, so a single
    #: noisy/outlier frame can't cause a wild jump or oscillation.
    max_step_factor: float = 2.0
    gain_step: int = 10
    #: Practical ceiling for a *live* view, independent of (and generally
    #: much lower than) the camera's own hardware max_exposure_ms — see
    #: the module docstring's "Live-view exposure ceiling".
    max_auto_exposure_ms: float = 2000.0


_DEFAULT_CONFIG = AutoExposureConfig()


@dataclass(frozen=True)
class AutoExposureResult:
    exposure_ms: float
    gain: int
    changed: bool
    #: Measured percentile-signal as a fraction of full ADU range (0..1),
    #: for display/diagnostics/tests.
    metric: float


def _measure(pixels: np.ndarray, bit_depth: int, percentile: float) -> float:
    """Fraction of *pixels*' assumed full range the frame's "signal"
    (a high percentile, not the mean) actually reaches.

    Real-hardware bug this saturation floor was added for: a camera
    whose true ADC range is smaller than the assumed bit_depth can sit
    fully saturated at its real ceiling while the naive
    `signal / (2**bit_depth - 1)` still reads as a small percentage.
    Auto-exposure would then conclude "far too dim" and escalate
    exposure/gain without bound, chasing a target the sensor can never
    produce — exactly what "guide stays black" turned out to be
    (saturated white, not empty; see also stretch_to_uint8's degenerate-
    uniform-frame handling for the other half of that bug). Confirmed
    on real hardware: gain 100->5000 left the frame completely
    unchanged, pinned at the sensor's true ceiling the whole time.

    The fraction of pixels sitting at the frame's own maximum is used as
    a *floor* on the naive metric, not a hard cutoff replacing it (an
    earlier version returned a hard 1.0 once >=50% of pixels were
    saturated, else fell through to the naive value alone — on real
    hardware, as exposure/gain corrections nudged the saturated fraction
    back and forth across that 50% line, the reported metric jumped
    between ~1.0 and ~0.0625 every other frame, and auto-exposure
    chased that discontinuity instead of settling: confirmed on the
    real GPCMOS02000KPA, oscillating indefinitely between ~1.5ms and
    ~4.5ms exposure). Taking the max of the two instead makes the
    metric rise smoothly as more of the frame clips, with no cliff to
    oscillate around: a small hot-pixel cluster or a synthetic test
    frame with only a handful of pixels at their own max barely moves
    it, while a real sensor increasingly pinned at its true ceiling
    pushes it smoothly toward 1.0.
    """
    if pixels.size == 0:
        return 0.0
    adu_max = float(2**bit_depth - 1)
    if adu_max <= 0.0:
        return 0.0
    signal = float(np.percentile(pixels, percentile))
    naive_metric = max(0.0, signal) / adu_max
    actual_max = float(pixels.max())
    if actual_max <= 0.0:
        return naive_metric
    saturated_fraction = float(np.mean(pixels >= actual_max * 0.999))
    return max(naive_metric, saturated_fraction)


def compute_auto_exposure(
    pixels: np.ndarray,
    *,
    bit_depth: int,
    current_exposure_ms: float,
    current_gain: int,
    capabilities: CameraCapabilities,
    config: AutoExposureConfig = _DEFAULT_CONFIG,
) -> AutoExposureResult:
    """Return the exposure/gain to use for the *next* frame.

    `changed=False` means the current settings already keep the signal in
    `[target_low, target_high]` — the caller need not touch the camera.
    """
    metric = _measure(pixels, bit_depth, config.percentile)

    if config.target_low <= metric <= config.target_high:
        return AutoExposureResult(current_exposure_ms, current_gain, changed=False, metric=metric)

    # The live-view ceiling, not the camera's own (often huge) hardware
    # max — see the module docstring's "Live-view exposure ceiling".
    effective_max_exposure_ms = min(capabilities.max_exposure_ms, config.max_auto_exposure_ms)

    target_mid = (config.target_low + config.target_high) / 2
    if metric <= 0.0:
        # Nothing to scale from (e.g. a fully black frame) — push exposure
        # up and let the next frame's measurement drive further changes.
        desired_exposure = effective_max_exposure_ms
    else:
        scale = target_mid / metric
        scale = min(config.max_step_factor, max(1.0 / config.max_step_factor, scale))
        desired_exposure = current_exposure_ms * scale

    clamped_exposure = min(
        effective_max_exposure_ms, max(capabilities.min_exposure_ms, desired_exposure)
    )

    too_dim = metric < config.target_low
    new_gain = current_gain
    if too_dim and desired_exposure > effective_max_exposure_ms:
        # Exposure is already at the live-view ceiling and still not
        # enough — only now step gain up.
        new_gain = min(capabilities.max_gain, current_gain + config.gain_step)
    elif not too_dim and desired_exposure < capabilities.min_exposure_ms:
        # Symmetric case: too bright even at minimum exposure.
        new_gain = max(capabilities.min_gain, current_gain - config.gain_step)

    changed = clamped_exposure != current_exposure_ms or new_gain != current_gain
    return AutoExposureResult(clamped_exposure, new_gain, changed=changed, metric=metric)
