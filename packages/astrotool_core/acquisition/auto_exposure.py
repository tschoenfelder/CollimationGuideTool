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

Gain unwinds back toward `default_gain` once conditions allow it, not just
up: real report (diagnostic fa1167b4) -- a rig can end up with gain raised
from an earlier dim/pinned-exposure correction, then exposure alone comes
back down to handle a brighter scene without ever revisiting that now-
unnecessary gain, leaving exposure/gain at a working point (e.g. 2ms at
gain 3990) that produces mostly amplified read noise instead of real
signal even though the *metric* itself reads fine. When a frame is
already in-band and `current_gain > default_gain`, gain is unwound while
exposure is scaled up to compensate (same linear signal-vs-exposure/gain
assumption the escalation path already makes), as long as the
compensated exposure still fits under the live-view ceiling -- so gain
only ever settles as low as exposure has genuine room to cover for. This
was originally scoped out ("revisit if it matters in practice" -- it now
does) rather than removed outright.

The unwind step itself halves gain (bounded by `max_step_factor`, the
same clamp exposure's own escalation already uses -- exposure doubling
to compensate is exactly as fast as escalation is ever allowed to grow
it) rather than a fixed `gain_step`: real follow-up report -- a fixed
step of 10 took over a thousand corrections to walk a gain elevated by
1000+ back down, visibly "barely moving". Halving instead converges in
single-digit corrections (13,140 -> ~100 in about 8) while staying just
as bounded per-step as escalation always is -- no single correction can
jump exposure straight to the live-view ceiling and freeze the view.

Gain step is adaptive, not a fixed +/-10 (real request: a fixed step either
crawls painfully slowly toward the target on a camera whose gain barely
moves the signal, or overshoots and oscillates on one where it moves it a
lot). `compute_auto_exposure` estimates the local sensitivity
(dmetric/dgain) from the *previous* correction's (metric, gain) pair versus
this one's, then solves directly for the gain change that should land the
metric at the target band's midpoint (secant-method style) rather than
guessing. Since this function is deliberately stateless (see below), the
caller supplies that previous pair explicitly via `previous_metric`/
`previous_gain` -- on the very first correction (no prior pair yet) or
whenever the last correction didn't actually change gain (a zero gain
delta would divide by ~zero), it falls back to the fixed
`AutoExposureConfig.gain_step` as a one-time probe, exactly like the old
behavior. The estimated step is clamped to `max_gain_step` either way, so
a noisy single-frame sensitivity estimate can't cause a wild jump, same
philosophy as exposure's own `max_step_factor`.

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

import math
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
    #: Fixed step used only when there's no usable prior (metric, gain)
    #: pair to estimate a sensitivity from -- see the module docstring's
    #: "Gain step is adaptive" section.
    gain_step: int = 10
    #: Clamp on the adaptive step's magnitude, so a noisy single-frame
    #: sensitivity estimate can't swing gain wildly in one correction --
    #: same philosophy as max_step_factor for exposure.
    max_gain_step: int = 50
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


def _adaptive_gain_step(
    metric: float,
    target_mid: float,
    current_gain: int,
    previous_metric: float | None,
    previous_gain: int | None,
    config: AutoExposureConfig,
) -> int:
    """Signed gain delta for one correction -- see the module docstring's
    "Gain step is adaptive" section. `metric` is guaranteed outside
    `[target_low, target_high]` by the only caller (`compute_auto_exposure`
    already returned early otherwise), so it's never exactly `target_mid`
    and the fixed-step fallback below always has a real direction to step
    in.
    """
    if (
        previous_metric is not None
        and previous_gain is not None
        and current_gain != previous_gain
    ):
        sensitivity = (metric - previous_metric) / (current_gain - previous_gain)
        if abs(sensitivity) > 1e-9:
            needed = (target_mid - metric) / sensitivity
            step = int(round(needed))
            step = max(-config.max_gain_step, min(config.max_gain_step, step))
            if step != 0:
                return step
    return config.gain_step if metric < target_mid else -config.gain_step


def _unwind_gain(
    current_exposure_ms: float,
    current_gain: int,
    effective_max_exposure_ms: float,
    config: AutoExposureConfig,
) -> tuple[float, int] | None:
    """Halves `current_gain` (clamped to `default_gain` and to whatever
    the live-view ceiling can actually compensate for), with
    `current_exposure_ms` scaled up to match -- see the module
    docstring's "Gain unwinds" and "unwind step itself halves" sections.
    Returns None (no-op) if gain is already at `default_gain`, or if the
    ceiling can't compensate for *any* reduction at all (gain stays right
    where it is rather than partially unwinding into an under-exposed
    frame the caller never asked for).
    """
    if current_gain <= config.default_gain:
        return None
    # Same linear signal-vs-exposure/gain assumption the escalation path
    # already makes (see compute_auto_exposure's own exposure `scale`
    # step) -- less gain needs proportionally more exposure to land the
    # same measured signal.
    halved = round(current_gain / config.max_step_factor)
    # The live-view ceiling may allow *less* of a drop than a plain
    # halving would need -- exposure can only compensate so far.
    # Compensating for a smaller gain always needs *more* exposure, never
    # less, so this can only ever raise the floor, never lower it.
    ceiling_floor = math.ceil(current_gain * current_exposure_ms / effective_max_exposure_ms)
    candidate_gain = max(config.default_gain, halved, ceiling_floor)
    if candidate_gain >= current_gain:
        return None
    candidate_exposure = min(
        effective_max_exposure_ms, current_exposure_ms * (current_gain / candidate_gain)
    )
    return (candidate_exposure, candidate_gain)


def compute_auto_exposure(
    pixels: np.ndarray,
    *,
    bit_depth: int,
    current_exposure_ms: float,
    current_gain: int,
    capabilities: CameraCapabilities,
    config: AutoExposureConfig = _DEFAULT_CONFIG,
    #: The (metric, gain) pair from the *previous* call this same caller
    #: made -- deliberately explicit rather than internal state, matching
    #: this function's existing "single stateless call" shape (see module
    #: docstring). None on a caller's first-ever call, or if a caller
    #: doesn't want the adaptive behavior (falls back to a fixed step
    #: every time, the original behavior). See "Gain step is adaptive".
    previous_metric: float | None = None,
    previous_gain: int | None = None,
) -> AutoExposureResult:
    """Return the exposure/gain to use for the *next* frame.

    `changed=False` means the current settings already keep the signal in
    `[target_low, target_high]` — the caller need not touch the camera.
    """
    metric = _measure(pixels, bit_depth, config.percentile)

    # The live-view ceiling, not the camera's own (often huge) hardware
    # max — see the module docstring's "Live-view exposure ceiling".
    effective_max_exposure_ms = min(capabilities.max_exposure_ms, config.max_auto_exposure_ms)

    if config.target_low <= metric <= config.target_high:
        unwound = _unwind_gain(current_exposure_ms, current_gain, effective_max_exposure_ms, config)
        if unwound is not None:
            new_exposure, new_gain = unwound
            return AutoExposureResult(new_exposure, new_gain, changed=True, metric=metric)
        return AutoExposureResult(current_exposure_ms, current_gain, changed=False, metric=metric)

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
        step = _adaptive_gain_step(
            metric, target_mid, current_gain, previous_metric, previous_gain, config
        )
        new_gain = min(capabilities.max_gain, current_gain + step)
    elif not too_dim and desired_exposure < capabilities.min_exposure_ms:
        # Symmetric case: too bright even at minimum exposure.
        step = _adaptive_gain_step(
            metric, target_mid, current_gain, previous_metric, previous_gain, config
        )
        new_gain = max(capabilities.min_gain, current_gain + step)

    changed = clamped_exposure != current_exposure_ms or new_gain != current_gain
    return AutoExposureResult(clamped_exposure, new_gain, changed=changed, metric=metric)
