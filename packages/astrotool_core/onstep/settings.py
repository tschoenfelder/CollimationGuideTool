"""OnStepAdapter >= 0.4.0's INDI runtime config -- AGENTS.md: indiserver is
the sole owner of the OnStep serial port for this deployment, and
OnStepAdapter itself talks INDI; nothing in this app opens the serial port
or a raw INDI connection directly.

`OnStepSettings`/`load_onstep_settings`/`build_onstep_safety_config`
(0.3.5's direct-serial config -- a port/baud pair plus an `OnStepSafetyConfig`)
were removed in this migration: `onstep_adapter.OnStepSafetyConfig` no
longer exists at all in >= 0.4.0 (the whole safety-config/serial-ownership
model it described belongs to the direct-serial transport this deployment
no longer uses), so keeping them was dead code that could not even import.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from onstep_adapter import IndiRuntimeConfig

DEFAULT_CONFIG_PATH = Path.home() / ".CollimationGuideTool" / "config.toml"
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


def _bool(table: dict[str, object], key: str, default: bool) -> bool:
    value = table.get(key)
    return value if isinstance(value, bool) else default


#: OnStepAdapter's own `derive_meridian_policy` requires
#: `0 < flip_request_deg < tracking_stop_deg < guard_deg`, where
#: `guard_deg` comes from the mount's live firmware readback
#: (`min(east, west guard minutes) / 4` -- see `meridian_policy.py`).
#: Real-rig measurement (rasppi3, 2026-09-23): that guard was only ~2.0
#: degrees (12/8 minutes East/West) -- an earlier 100.0/110.0 "conservative"
#: default here was actually never satisfiable on real hardware (it
#: refused to connect at all: "Configured flip/stop must be ordered below
#: both firmware guards"). These are deliberately tiny instead, chosen to
#: clear almost any real guard rather than to be a well-tuned safety
#: margin for a specific rig -- **every real deployment still needs its
#: own measured `[meridian]` override** (see install.md); this only
#: prevents an unconfigured install from failing to connect at all.
_DEFAULT_FLIP_REQUEST_DEG = 0.5
_DEFAULT_TRACKING_STOP_DEG = 0.75


#: OnStepAdapter >= 0.4.1: "strict" (default) makes `enable_tracking()`
#: require a completed HOME slew, a time/site sync and not-at-home -- none of
#: which this app can ever provide (it never calls go_home()/
#: sync_time_location()); "controller_managed" leaves those to the mount
#: controller and only refuses parked/slewing/limit/meridian-unsafe states.
_TRACKING_POLICIES = ("strict", "controller_managed")


def _tracking_policy(table: dict[str, object]) -> str:
    value = table.get("tracking_authority_policy")
    return value if isinstance(value, str) and value in _TRACKING_POLICIES else "strict"


def load_onstep_indi_config(
    smarttscope_path: Path | None = None, own_path: Path | None = None
) -> IndiRuntimeConfig:
    """OnStepAdapter >= 0.4.0's INDI runtime config.

    The observer site is the same `[observer]` table this module always
    read from `~/.SmartTScope/config.toml` -- never re-invented here. The
    INDI connection identity (`[indi]`: host/port/device/
    home_motion_enabled/focuser_max_position) and the meridian supervisor's
    degree limits (`[meridian]`) have no SmartTScope equivalent (that
    project owns its own, separate INDI/serial wiring) and live only in
    this app's own `~/.CollimationGuideTool/config.toml`. Missing
    file/table/malformed value -> the defaults below, never an error.
    """
    site = _table(smarttscope_path or SMARTTSCOPE_CONFIG_PATH, "observer")
    indi = _table(own_path or DEFAULT_CONFIG_PATH, "indi")
    meridian = _table(own_path or DEFAULT_CONFIG_PATH, "meridian")
    focuser_max = indi.get("focuser_max_position")
    return IndiRuntimeConfig(
        host=_text(indi, "host", "127.0.0.1"),
        port=int(_number(indi, "port", 7624)),
        device=_text(indi, "device", "LX200 OnStep"),
        observer_lat=_number(site, "lat", 50.336),
        observer_lon=_number(site, "lon", 8.533),
        observer_alt_m=_number(site, "height_m", 0.0),
        flip_request_deg=_number(meridian, "flip_request_deg", _DEFAULT_FLIP_REQUEST_DEG),
        tracking_stop_deg=_number(meridian, "tracking_stop_deg", _DEFAULT_TRACKING_STOP_DEG),
        flip_allowance_seconds=_number(meridian, "flip_allowance_seconds", 120.0),
        reserve_seconds=_number(meridian, "reserve_seconds", 30.0),
        safe_meridian_flip_via_home=_bool(indi, "safe_meridian_flip_via_home", True),
        focuser_max_position=(
            int(focuser_max) if isinstance(focuser_max, int) and not isinstance(focuser_max, bool)
            else None
        ),
        home_motion_enabled=_bool(indi, "home_motion_enabled", False),
        tracking_authority_policy=_tracking_policy(indi),
    )
