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
    adu_max = float(2**bit_depth - 1)
    if adu_max <= 0.0 or pixels.size == 0:
        return 0.0
    signal = float(np.percentile(pixels, percentile))
    return max(0.0, signal) / adu_max


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

    target_mid = (config.target_low + config.target_high) / 2
    if metric <= 0.0:
        # Nothing to scale from (e.g. a fully black frame) — push exposure
        # up and let the next frame's measurement drive further changes.
        desired_exposure = capabilities.max_exposure_ms
    else:
        scale = target_mid / metric
        scale = min(config.max_step_factor, max(1.0 / config.max_step_factor, scale))
        desired_exposure = current_exposure_ms * scale

    clamped_exposure = min(
        capabilities.max_exposure_ms, max(capabilities.min_exposure_ms, desired_exposure)
    )

    too_dim = metric < config.target_low
    new_gain = current_gain
    if too_dim and desired_exposure > capabilities.max_exposure_ms:
        # Exposure is already maxed and still not enough — only now step gain up.
        new_gain = min(capabilities.max_gain, current_gain + config.gain_step)
    elif not too_dim and desired_exposure < capabilities.min_exposure_ms:
        # Symmetric case: too bright even at minimum exposure.
        new_gain = max(capabilities.min_gain, current_gain - config.gain_step)

    changed = clamped_exposure != current_exposure_ms or new_gain != current_gain
    return AutoExposureResult(clamped_exposure, new_gain, changed=changed, metric=metric)
