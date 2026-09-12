"""Mount-alignment feature settings ("Run Calibration" + the per-camera
direction pads in `MountTestMovePanel`).

Requested directly: the calibration slew duration/rate and each direction
button's target nudge size should be fixed constants, not a runtime UI
control, but sourced from config rather than hardcoded in the panel — so a
rig-specific value can be tuned (e.g. a different slew-rate preset, or a
gentler nudge) by editing `~/.CollimationGuideTool/config.toml` directly,
without a code change.

Written to (read from, never written by this app — there is deliberately no
UI to change these) the `[mount_alignment]` table in the same shared
`~/.CollimationGuideTool/config.toml` as `camera_settings.py`'s
`[cameras.<panel_name>]` tables. Same hand-rolled-`tomllib`-read, same
"missing file/table/malformed value -> defaults, never an error" tolerance
as that module — see its docstring for why a dependency isn't warranted
here either.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".CollimationGuideTool" / "config.toml"

#: IndiMountPulseAdapter's probed TELESCOPE_SLEW_RATE table: element "7" is
#: "48x" -- see that module's docstring. Kept as the default here so the
#: calibration/nudge pulses run faster than the adapter's own "20x" default
#: (chosen for a quick, easily-detected test move) without changing that
#: adapter's default for any other caller.
_DEFAULT_RATE_PRESET = "7"
_DEFAULT_PULSE_MS = 1000
#: Real request: nudges should move a large, decisive distance for rough
#: alignment ("the buttons should move half a window in the direction
#: specified"), not a small fixed pixel count -- a future "slow down
#: near target" fine-adjustment mode is explicitly deferred, not this.
#: Fraction of *this camera's own* frame width, used uniformly for
#: RA+/RA-/Dec+/Dec- alike (see MountTestMovePanel._on_nudge_clicked's
#: own docstring for why it has to be a fraction of the actual frame,
#: not a fixed pixel count, given Main and Guide have very different
#: resolutions).
_DEFAULT_NUDGE_TARGET_FRACTION = 0.5
#: Real report: "calibration doesn't wait for mount to be stabilized" --
#: MountTestMoveRunner used to capture the "after" frame the instant
#: pulse_axis() confirmed the motion switch back off, with no allowance
#: for mechanical settle (backlash/vibration damping out) between the
#: motor physically stopping and the mount actually being at rest. See
#: MountTestMoveRunner's own docstring for where this is applied.
#: Real follow-up request: "if the calibration runs for x ms, ... take a
#: frame after x + 1 sec ... for the telescope to stop" -- raised from
#: the original 300ms guess to a full second.
_DEFAULT_SETTLE_MS = 1000
#: Real report: "still 2-3 frames are shown showing movement" after a
#: pulse -- a single frame delivered past the pulse-completion reference
#: isn't strong enough evidence the mount has actually finished
#: mechanically settling (residual vibration/backlash damping out can
#: outlast settle_ms above and the very first fresh-delivered frame
#: both). User's own recipe: "check on mount being stopped first, grant
#: the 500ms and take frame then only" -- see
#: MountTestMovePanel._capture_both's own docstring for where this is
#: applied: once the stream first confirms it's caught up past the
#: pulse, wait this much *again*, then take the frame actually used for
#: measurement from *that* point on, not the first barely-fresh one.
_DEFAULT_FRAME_SETTLE_MS = 500
#: Real report (diagnostic de295656): "Guide showing buttons, but
#: movement far too much" -- the pulse duration solved from a click's own
#: calibrated rate had no cap at all, linearly extrapolating from the
#: pulse_ms-long calibration rate out to whatever duration a nudge's
#: target displacement needs. For an axis with a slow calibrated rate
#: (Guide's own AXIS2 that run: 13px per 500ms), a half-window target
#: solved to 20+ real seconds -- silently clamped down to
#: IndiMountPulseAdapter's own hardware ceiling (9999ms) with no warning,
#: and even *that* clamped pulse produced far more real motion than the
#: short calibration pulse's rate predicted (extrapolating a rate that
#: far out isn't reliable on real hardware -- acceleration ramp-up
#: dominates a short pulse's own average rate). The target itself
#: (nudge_target_fraction of the frame) is already known before any
#: duration math runs, so an unreasonably long solved pulse is
#: detectable before ever starting the move, not only after the
#: driver-level clamp already silently changed what got sent.
#:
#: Real report, diagnostic 6cb859d2 (Guide's own AXIS2 hit this same
#: 13px-per-500ms case again): exceeding this cap used to refuse the
#: whole move outright, with the shown message itself suggesting
#: "...click again for a smaller step" -- but nothing about clicking the
#: same button again produced a smaller step (nudge_target_fraction is
#: fixed, so a second click solved for the identical target and hit the
#: identical refusal every time). `MountTestMovePanel._on_nudge_clicked`
#: now clamps the solved duration down to this cap instead, moving as far
#: as safely possible this click -- clicking again genuinely continues
#: toward the original target. Independently tunable like every other
#: setting here; needing several clicks to reach one target is itself a
#: signal that axis's calibrated rate is unreliably slow -- consider Run
#: Calibration again with a longer pulse_ms instead of raising this cap.
_DEFAULT_MAX_NUDGE_PULSE_MS = 3000
#: Issue #30: the largest consecutive-frame displacement (px) still treated
#: as "stable enough to measure" -- see `astrotool_core.acquisition.
#: image_stability.check_image_stability`'s own tolerance_px parameter,
#: which this feeds directly. A starting value, not yet live-verified: sits
#: inside the one piece of real evidence gathered so far
#: (local_test_data/28_corpus/stationary/, captured 2026-09-11) -- Guide
#: read an exact (0,0) across all 45 real stationary pairs, Main showed
#: 0-5px of real outdoor-daytime environmental drift while genuinely
#: mount-untouched. Tight enough to catch real instability, loose enough
#: not to misclassify Main's own baseline noise as unstable forever. Needs
#: tuning against a live "Run Calibration" attempt before being trusted,
#: same as every other real-hardware-facing constant in this module.
_DEFAULT_STABILITY_TOLERANCE_PX = 3.0
#: Matches `acquisition.motion_aware_acquisition.acquire_verified_frame`'s
#: own kwarg default -- no evidence yet to diverge from it.
_DEFAULT_STABILITY_SAMPLE_COUNT = 3
_DEFAULT_STABILITY_SAMPLE_INTERVAL_S = 0.2
#: Issue #30 open question #4 ("max allowed wait for stability, bounded,
#: cancellable"). Large enough to cover frame_settle_ms (1s default) plus
#: several stability-window retries at typical short exposures; small
#: enough that a genuinely wind-disturbed session fails explicitly within
#: single-digit seconds instead of hanging the whole calibration sequence.
#: A starting value, not yet live-verified -- see stability_tolerance_px's
#: own docstring for the same caveat.
_DEFAULT_STABILITY_TIMEOUT_S = 8.0
#: Issue #31's own suggested frame-relative movement sizes for the
#: per-camera screen-relative ←→↑↓ controls, once a valid response model
#: exists ("Small ≈ 5% of this camera's frame, Medium ≈ 15%, Large ≈
#: 30%") -- these are the issue's own suggested starting percentages, not
#: yet tuned against a live rig, same caveat as every other real-
#: hardware-facing constant in this module. Deliberately a fraction of
#: *this camera's own* frame width (mirrors `nudge_target_fraction`'s own
#: reasoning), not a fixed pixel count -- Main and Guide have very
#: different resolutions.
_DEFAULT_SCREEN_MOVE_SMALL_FRACTION = 0.05
_DEFAULT_SCREEN_MOVE_MEDIUM_FRACTION = 0.15
_DEFAULT_SCREEN_MOVE_LARGE_FRACTION = 0.30


@dataclass(frozen=True)
class MountAlignmentSettings:
    """`pulse_ms`/`rate_preset` are used for every calibration test pulse
    (the return pulse reuses the same values, trusting a symmetric
    response). `nudge_target_fraction` is the displacement a single
    RA+/RA-/Dec+/Dec- direction-pad click aims for, as a fraction of the
    clicked camera's own frame width, used the same way for either axis
    (see `MountTestMovePanel._on_nudge_clicked`'s own docstring for why a
    direct single-axis move has no natural "which screen dimension" the
    way an earlier composed screen-relative move did); the click's own
    calibrated rate for that one axis (`AxisResponse.magnitude_px /
    duration_ms`) solves the pulse duration that should produce it.
    `settle_ms` is how long MountTestMoveRunner waits after a pulse
    physically stops before reporting done -- see that module's own
    docstring. `frame_settle_ms` is a *second*, camera-side buffer on top
    of that: how long MountTestMovePanel waits again, after the video
    stream first confirms it has caught up past the pulse, before
    actually taking the frame used for measurement -- see that panel's
    own `_capture_both` docstring. `max_nudge_pulse_ms` caps how long a
    single nudge pulse is allowed to run -- a solved duration exceeding
    it is clamped down to land exactly on this cap rather than refused
    outright -- see that constant's own docstring.

    Issue #30, the deeper image-stability layer `_capture_both` verifies
    every before/after frame against (on top of `frame_settle_ms`'s own
    fixed buffer, which stays a lower bound, not proof of stability):
    `stability_tolerance_px` is the largest consecutive-frame displacement
    still treated as stable; `stability_sample_count` is how many
    consecutive frames must agree within that tolerance before a capture
    is accepted; `stability_sample_interval_s` is the wait between drawing
    each fresh sample in that window; `stability_timeout_s` bounds the
    whole wait so a genuinely wind-disturbed or never-settling session
    fails explicitly instead of hanging. See each constant's own
    docstring for the real evidence behind its starting value.

    Issue #31: `screen_move_small_fraction`/`_medium_fraction`/
    `_large_fraction` are the frame-relative movement sizes a per-camera
    screen-relative ←→↑↓ control offers once that camera has a valid,
    non-degenerate 4-direction response model -- each is a fraction of
    *that camera's own* frame width/height, mirroring
    `nudge_target_fraction`'s own reasoning. `max_nudge_pulse_ms` (above)
    doubles as these controls' own safety cap too -- no separate setting,
    same clamp-not-refuse philosophy `_on_nudge_clicked` already uses."""

    pulse_ms: int = _DEFAULT_PULSE_MS
    rate_preset: str = _DEFAULT_RATE_PRESET
    nudge_target_fraction: float = _DEFAULT_NUDGE_TARGET_FRACTION
    settle_ms: int = _DEFAULT_SETTLE_MS
    frame_settle_ms: int = _DEFAULT_FRAME_SETTLE_MS
    max_nudge_pulse_ms: int = _DEFAULT_MAX_NUDGE_PULSE_MS
    stability_tolerance_px: float = _DEFAULT_STABILITY_TOLERANCE_PX
    stability_sample_count: int = _DEFAULT_STABILITY_SAMPLE_COUNT
    stability_sample_interval_s: float = _DEFAULT_STABILITY_SAMPLE_INTERVAL_S
    stability_timeout_s: float = _DEFAULT_STABILITY_TIMEOUT_S
    screen_move_small_fraction: float = _DEFAULT_SCREEN_MOVE_SMALL_FRACTION
    screen_move_medium_fraction: float = _DEFAULT_SCREEN_MOVE_MEDIUM_FRACTION
    screen_move_large_fraction: float = _DEFAULT_SCREEN_MOVE_LARGE_FRACTION


def load_mount_alignment_settings(
    path: Path | str = DEFAULT_CONFIG_PATH,
) -> MountAlignmentSettings:
    """Read the `[mount_alignment]` table, falling back to
    `MountAlignmentSettings()`'s defaults for a missing file, missing table,
    or any malformed/missing individual value -- a convenience override,
    never required state (same contract as `load_camera_settings`)."""
    defaults = MountAlignmentSettings()
    try:
        with Path(path).open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return defaults

    table = data.get("mount_alignment")
    if not isinstance(table, dict):
        return defaults

    try:
        pulse_ms = int(table.get("pulse_ms", defaults.pulse_ms))
    except (TypeError, ValueError):
        pulse_ms = defaults.pulse_ms

    rate_preset_value = table.get("rate_preset", defaults.rate_preset)
    rate_preset = str(rate_preset_value) if rate_preset_value is not None else defaults.rate_preset

    try:
        nudge_target_fraction = float(
            table.get("nudge_target_fraction", defaults.nudge_target_fraction)
        )
    except (TypeError, ValueError):
        nudge_target_fraction = defaults.nudge_target_fraction

    try:
        settle_ms = int(table.get("settle_ms", defaults.settle_ms))
    except (TypeError, ValueError):
        settle_ms = defaults.settle_ms

    try:
        frame_settle_ms = int(table.get("frame_settle_ms", defaults.frame_settle_ms))
    except (TypeError, ValueError):
        frame_settle_ms = defaults.frame_settle_ms

    try:
        max_nudge_pulse_ms = int(table.get("max_nudge_pulse_ms", defaults.max_nudge_pulse_ms))
    except (TypeError, ValueError):
        max_nudge_pulse_ms = defaults.max_nudge_pulse_ms

    try:
        stability_tolerance_px = float(
            table.get("stability_tolerance_px", defaults.stability_tolerance_px)
        )
    except (TypeError, ValueError):
        stability_tolerance_px = defaults.stability_tolerance_px

    try:
        stability_sample_count = int(
            table.get("stability_sample_count", defaults.stability_sample_count)
        )
    except (TypeError, ValueError):
        stability_sample_count = defaults.stability_sample_count

    try:
        stability_sample_interval_s = float(
            table.get("stability_sample_interval_s", defaults.stability_sample_interval_s)
        )
    except (TypeError, ValueError):
        stability_sample_interval_s = defaults.stability_sample_interval_s

    try:
        stability_timeout_s = float(
            table.get("stability_timeout_s", defaults.stability_timeout_s)
        )
    except (TypeError, ValueError):
        stability_timeout_s = defaults.stability_timeout_s

    try:
        screen_move_small_fraction = float(
            table.get("screen_move_small_fraction", defaults.screen_move_small_fraction)
        )
    except (TypeError, ValueError):
        screen_move_small_fraction = defaults.screen_move_small_fraction

    try:
        screen_move_medium_fraction = float(
            table.get("screen_move_medium_fraction", defaults.screen_move_medium_fraction)
        )
    except (TypeError, ValueError):
        screen_move_medium_fraction = defaults.screen_move_medium_fraction

    try:
        screen_move_large_fraction = float(
            table.get("screen_move_large_fraction", defaults.screen_move_large_fraction)
        )
    except (TypeError, ValueError):
        screen_move_large_fraction = defaults.screen_move_large_fraction

    return MountAlignmentSettings(
        pulse_ms=pulse_ms,
        rate_preset=rate_preset,
        nudge_target_fraction=nudge_target_fraction,
        settle_ms=settle_ms,
        frame_settle_ms=frame_settle_ms,
        max_nudge_pulse_ms=max_nudge_pulse_ms,
        stability_tolerance_px=stability_tolerance_px,
        stability_sample_count=stability_sample_count,
        stability_sample_interval_s=stability_sample_interval_s,
        stability_timeout_s=stability_timeout_s,
        screen_move_small_fraction=screen_move_small_fraction,
        screen_move_medium_fraction=screen_move_medium_fraction,
        screen_move_large_fraction=screen_move_large_fraction,
    )
