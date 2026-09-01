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
_DEFAULT_NUDGE_TARGET_PX = 10.0
#: Real report: "calibration doesn't wait for mount to be stabilized" --
#: MountTestMoveRunner used to capture the "after" frame the instant
#: pulse_axis() confirmed the motion switch back off, with no allowance
#: for mechanical settle (backlash/vibration damping out) between the
#: motor physically stopping and the mount actually being at rest. See
#: MountTestMoveRunner's own docstring for where this is applied.
_DEFAULT_SETTLE_MS = 300


@dataclass(frozen=True)
class MountAlignmentSettings:
    """`pulse_ms`/`rate_preset` are used for every calibration test pulse
    (the return pulse reuses the same values, trusting a symmetric
    response). `nudge_target_px` is the on-screen displacement a single
    direction-pad click aims for; `compose_screen_move` solves the
    (axis1_ms, axis2_ms) pulse pair that should produce it for that
    camera's own calibration. `settle_ms` is how long MountTestMoveRunner
    waits after a pulse (or composed sequence of pulses) physically stops
    before reporting done -- see that module's own docstring."""

    pulse_ms: int = _DEFAULT_PULSE_MS
    rate_preset: str = _DEFAULT_RATE_PRESET
    nudge_target_px: float = _DEFAULT_NUDGE_TARGET_PX
    settle_ms: int = _DEFAULT_SETTLE_MS


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
        nudge_target_px = float(table.get("nudge_target_px", defaults.nudge_target_px))
    except (TypeError, ValueError):
        nudge_target_px = defaults.nudge_target_px

    try:
        settle_ms = int(table.get("settle_ms", defaults.settle_ms))
    except (TypeError, ValueError):
        settle_ms = defaults.settle_ms

    return MountAlignmentSettings(
        pulse_ms=pulse_ms,
        rate_preset=rate_preset,
        nudge_target_px=nudge_target_px,
        settle_ms=settle_ms,
    )
