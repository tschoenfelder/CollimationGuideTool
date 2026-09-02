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
#: Fraction of *this camera's own* frame width (Left/Right) or height
#: (Up/Down) -- see MountTestMovePanel._on_nudge_clicked's own docstring
#: for why it has to be a fraction of the actual frame, not a fixed
#: pixel count, given Main and Guide have very different resolutions.
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
#: movement far too much" -- compose_screen_move() had no cap at all on
#: the pulse duration it solves for, linearly extrapolating from the
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
#: detectable -- and refusable -- before ever starting the move, not
#: only after the driver-level clamp already silently changed what got
#: sent. See MountTestMovePanel._on_nudge_clicked's own docstring for
#: where this is checked. Independently tunable like every other setting
#: here; hitting it repeatedly for one axis is itself a signal that
#: axis's calibrated rate is unreliably slow -- consider Run Calibration
#: again with a longer pulse_ms instead of raising this cap.
_DEFAULT_MAX_NUDGE_PULSE_MS = 3000


@dataclass(frozen=True)
class MountAlignmentSettings:
    """`pulse_ms`/`rate_preset` are used for every calibration test pulse
    (the return pulse reuses the same values, trusting a symmetric
    response). `nudge_target_fraction` is the on-screen displacement a
    single direction-pad click aims for, as a fraction of the clicked
    camera's own frame width (Left/Right) or height (Up/Down);
    `compose_screen_move` solves the (axis1_ms, axis2_ms) pulse pair that
    should produce it for that camera's own calibration. `settle_ms` is
    how long MountTestMoveRunner waits after a pulse (or composed
    sequence of pulses) physically stops before reporting done -- see
    that module's own docstring. `frame_settle_ms` is a *second*,
    camera-side buffer on top of that: how long MountTestMovePanel waits
    again, after the video stream first confirms it has caught up past
    the pulse, before actually taking the frame used for measurement --
    see that panel's own `_capture_both` docstring. `max_nudge_pulse_ms`
    caps how long a single composed nudge pulse is allowed to solve for --
    see that constant's own docstring."""

    pulse_ms: int = _DEFAULT_PULSE_MS
    rate_preset: str = _DEFAULT_RATE_PRESET
    nudge_target_fraction: float = _DEFAULT_NUDGE_TARGET_FRACTION
    settle_ms: int = _DEFAULT_SETTLE_MS
    frame_settle_ms: int = _DEFAULT_FRAME_SETTLE_MS
    max_nudge_pulse_ms: int = _DEFAULT_MAX_NUDGE_PULSE_MS


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

    return MountAlignmentSettings(
        pulse_ms=pulse_ms,
        rate_preset=rate_preset,
        nudge_target_fraction=nudge_target_fraction,
        settle_ms=settle_ms,
        frame_settle_ms=frame_settle_ms,
        max_nudge_pulse_ms=max_nudge_pulse_ms,
    )
