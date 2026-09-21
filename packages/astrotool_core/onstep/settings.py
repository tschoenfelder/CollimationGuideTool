"""Where the one OnStep serial port is: the `[onstep]` table of
`~/.CollimationGuideTool/config.toml`, overridable by the `ONSTEP_PORT`
environment variable (the same variable SmartTScope uses; the default is the rig's udev symlink
SmartTScope uses).

This app never opens that port itself: `OnStepConnection` hands it to
OnStepAdapter's `OnStepClient`, the only OnStep connection allowed
(AGENTS.md, OnStepAdapter >= 0.3.5 ownership contract).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from onstep_adapter import OnStepSafetyConfig

DEFAULT_CONFIG_PATH = Path.home() / ".CollimationGuideTool" / "config.toml"
DEFAULT_SERIAL_PORT = "/dev/ttyUSB_ONSTEP0"
DEFAULT_BAUD_RATE = 9600


@dataclass(frozen=True)
class OnStepSettings:
    serial_port: str = DEFAULT_SERIAL_PORT
    baud_rate: int = DEFAULT_BAUD_RATE


def load_onstep_settings(
    path: Path | None = None, environ: dict[str, str] | None = None
) -> OnStepSettings:
    """Missing file/table/malformed value -> defaults, never an error."""
    env = os.environ if environ is None else environ
    table: dict[str, object] = {}
    try:
        with open(path or DEFAULT_CONFIG_PATH, "rb") as f:
            loaded = tomllib.load(f).get("onstep", {})
        if isinstance(loaded, dict):
            table = loaded
    except (OSError, tomllib.TOMLDecodeError):
        pass
    port = env.get("ONSTEP_PORT") or table.get("serial_port")
    baud = table.get("baud_rate")
    return OnStepSettings(
        serial_port=port if isinstance(port, str) and port else DEFAULT_SERIAL_PORT,
        baud_rate=baud
        if isinstance(baud, int) and not isinstance(baud, bool) and baud > 0
        else DEFAULT_BAUD_RATE,
    )


SMARTTSCOPE_CONFIG_PATH = Path.home() / ".SmartTScope" / "config.toml"


def _table(path: Path, name: str) -> dict[str, object]:
    try:
        with open(path, "rb") as f:
            loaded = tomllib.load(f).get(name, {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _number(table: dict[str, object], key: str, default: float) -> float:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def _text(table: dict[str, object], key: str, default: str) -> str:
    value = table.get(key)
    return value if isinstance(value, str) and value else default


def build_onstep_safety_config(
    smarttscope_path: Path | None = None, own_path: Path | None = None
) -> OnStepSafetyConfig:
    """The site and mount limits OnStepAdapter's safety layer checks every motion against.

    Without them the adapter runs "unconfigured" (observer 0/0, no trusted clock) and refuses
    to move. The site and limits come from the rig's existing `~/.SmartTScope/config.toml`
    (`[observer] lat/lon`, `[mount_limits]`, the same file the optics come from); the state
    files live beside SmartTScope's so both applications see one home/park authority. Home
    confirmation stays REQUIRED -- it is an explicit operator action in the Mount panel.
    `[onstep] time_trust_source` (default `raspberry_plausible`) declares how far the Pi clock
    is trusted (`ntp`, `gps`, `rtc`, `user_confirmed`): OnStepAdapter's angular center moves
    are astronomy-grade and refuse a clock that is only plausible, so a rig whose clock IS
    disciplined (NTP/GPS) should say so.
    """
    site = _table(smarttscope_path or SMARTTSCOPE_CONFIG_PATH, "observer")
    limits = _table(smarttscope_path or SMARTTSCOPE_CONFIG_PATH, "mount_limits")
    own = _table(own_path or DEFAULT_CONFIG_PATH, "onstep")
    state_dir = Path.home() / ".SmartTScope"
    horizon = state_dir / "horizon.dat"
    return OnStepSafetyConfig(
        observer_lat=_number(own, "observer_lat", _number(site, "lat", 50.336)),
        observer_lon=_number(own, "observer_lon", _number(site, "lon", 8.533)),
        observer_alt_m=_number(own, "observer_alt_m", _number(site, "height_m", 0.0)),
        min_alt_deg=_number(limits, "min_alt_deg", 10.0),
        max_alt_deg=_number(limits, "max_alt_deg", 88.0),
        ha_east_limit_h=_number(limits, "ha_east_limit_h", -5.5),
        ha_west_limit_h=_number(limits, "ha_west_limit_h", 0.333),
        horizon_path=str(horizon) if horizon.exists() else "",
        state_file=str(state_dir / "onstep_last_state.json"),
        mechanical_calibration_file=str(state_dir / "onstep_calibration.json"),
        require_home_confirmation=True,
        time_trust_source=_text(own, "time_trust_source", "raspberry_plausible"),
        allow_broad_onstep_limits=True,
    )
