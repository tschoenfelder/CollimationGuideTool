"""Where the filter wheel's wiring facts actually live: the sibling
SmartTScope project's shared `~/.SmartTScope/config.toml`, not a parallel
CollimationGuideTool-invented schema for the same physical hardware.

Verified directly against the real rig (issue #47): that file's
`[filter_wheel]` table (singular -- there is only ever one physical wheel)
says `active_camera_role = "main"` -- the wheel is in Main's optical path
right now, not shared with OAG as this project's own earlier registry code
assumed -- and its `[filters]` table already names each slot
(`luminance = 1`, `red = 2`, ...). CollimationGuideTool reads both instead of
maintaining its own copy.

Where SmartTScope's config is unavailable (this dev machine, or any install
without SmartTScope), CollimationGuideTool falls back to the SAME table
shapes in its OWN `~/.CollimationGuideTool/config.toml` -- not a differently
shaped table -- extended with the one thing the shared schema can never carry
(SmartTScope talks to this wheel over its own native SDK, never INDI): the
INDI connection identity (`device`/`host`/`port`).

Resolution order, per file: a MISSING `[filter_wheel]` table (or an
unreadable file) means "consult the next source" -- SmartTScope's file, then
CollimationGuideTool's own, then a built-in default matching this rig's
known-good state. A table that IS present is authoritative for that source,
even if it says `enabled = false` or omits `active_camera_role` -- that is
an explicit "no wheel right now", never silently overridden by a later,
less-specific source. Never raises, matching every other config reader in
this package.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_SMARTTSCOPE_CONFIG_PATH = Path.home() / ".SmartTScope" / "config.toml"
DEFAULT_LOCAL_CONFIG_PATH = Path.home() / ".CollimationGuideTool" / "config.toml"

#: `[filters]`'s descriptive keys -> the short codes shown in the UI. A key
#: not in this table is skipped (never guessed at) -- see _read_filter_names.
_FILTER_NAME_ABBREVIATIONS: dict[str, str] = {
    "luminance": "L",
    "red": "R",
    "green": "G",
    "blue": "B",
    "ha": "H",
    "oiii": "O",
    "sii": "S",
}

#: This rig's own known-good state, used only when NEITHER config file
#: carries an explicit INDI `device` name. Corrected 2026-09-24 -- the
#: original "ToupTek EFW 1" guess was never checked against the real
#: indiserver (`indi_getprop`); a real-field UI failure ("EFW 1" not
#: found) traced to this constant, and the rig's actual driver reports
#: itself as "ToupTek EFW 2".
_BUILT_IN_DEVICE_NAME = "ToupTek EFW 2"
_BUILT_IN_ACTIVE_TRAIN = "main"
_BUILT_IN_FILTER_NAMES: dict[int, str] = {1: "L", 2: "R", 3: "G", 4: "B", 5: "H", 6: "O", 7: "S"}


@dataclass(frozen=True)
class FilterWheelWiring:
    enabled: bool
    #: e.g. "main"; None if disabled or no config says otherwise.
    active_train: str | None
    #: slot -> short code (e.g. {2: "R"}); {} if [filters] has nothing usable.
    filter_names: dict[int, str]
    #: INDI connection identity -- only ever populated from the LOCAL file
    #: (or the built-in default); the shared SmartTScope schema has no INDI
    #: device for this wheel (backend="native" there).
    device_name: str | None
    host: str | None
    port: int | None


def _load_toml(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
            return data
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _read_filter_names(data: dict[str, Any]) -> dict[int, str]:
    table = data.get("filters")
    if not isinstance(table, dict):
        return {}
    names: dict[int, str] = {}
    for key, value in table.items():
        code = _FILTER_NAME_ABBREVIATIONS.get(str(key).lower())
        if code is None:
            continue  # an unknown descriptive name is never guessed at
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        names[value] = code
    return names


def _read_filter_wheel_table(data: dict[str, Any]) -> tuple[bool, str | None] | None:
    """`(enabled, active_camera_role)`, or None if `[filter_wheel]` itself is
    absent -- the caller distinguishes "absent, keep looking" from "present,
    authoritative" (see module docstring)."""
    table = data.get("filter_wheel")
    if not isinstance(table, dict):
        return None
    enabled = table.get("enabled") is True
    role = table.get("active_camera_role")
    return enabled, (role if isinstance(role, str) and role else None)


def _read_indi_identity(data: dict[str, Any]) -> tuple[str | None, str | None, int | None]:
    """`(device, host, port)` -- only ever meaningful on the LOCAL file's
    `[filter_wheel]` table; the shared SmartTScope schema has no such keys."""
    table = data.get("filter_wheel")
    if not isinstance(table, dict):
        return None, None, None
    device = table.get("device")
    host = table.get("host")
    port = table.get("port")
    return (
        device if isinstance(device, str) and device else None,
        host if isinstance(host, str) and host else None,
        port if isinstance(port, int) and not isinstance(port, bool) else None,
    )


def load_filter_wheel_wiring(
    *,
    smarttscope_path: Path | str = DEFAULT_SMARTTSCOPE_CONFIG_PATH,
    local_path: Path | str = DEFAULT_LOCAL_CONFIG_PATH,
) -> FilterWheelWiring:
    """The wheel's wiring: which optical train it currently serves, and its
    per-slot filter names. Reads the shared SmartTScope config first (the
    authoritative source for a rig that has one); falls back to the SAME
    table shapes in CollimationGuideTool's own config; falls back to this
    rig's built-in known-good default if neither file has a `[filter_wheel]`
    table at all. Never raises."""
    local = _load_toml(Path(local_path))
    local_device, local_host, local_port = _read_indi_identity(local or {})

    shared = _load_toml(Path(smarttscope_path))
    if shared is not None:
        resolved = _read_filter_wheel_table(shared)
        if resolved is not None:
            enabled, active_train = resolved
            if enabled and active_train is not None:
                return FilterWheelWiring(
                    enabled=True,
                    active_train=active_train,
                    filter_names=_read_filter_names(shared),
                    device_name=local_device or _BUILT_IN_DEVICE_NAME,
                    host=local_host,
                    port=local_port,
                )
            # Table present but disabled (or no role) -- an explicit "no
            # wheel right now", authoritative, never overridden by a
            # less-specific source.
            return FilterWheelWiring(
                enabled=False,
                active_train=None,
                filter_names={},
                device_name=None,
                host=None,
                port=None,
            )

    if local is not None:
        resolved = _read_filter_wheel_table(local)
        if resolved is not None:
            enabled, active_train = resolved
            if enabled and active_train is not None:
                return FilterWheelWiring(
                    enabled=True,
                    active_train=active_train,
                    filter_names=_read_filter_names(local),
                    device_name=local_device or _BUILT_IN_DEVICE_NAME,
                    host=local_host,
                    port=local_port,
                )
            return FilterWheelWiring(
                enabled=False,
                active_train=None,
                filter_names={},
                device_name=None,
                host=None,
                port=None,
            )

    return FilterWheelWiring(
        enabled=True,
        active_train=_BUILT_IN_ACTIVE_TRAIN,
        filter_names=dict(_BUILT_IN_FILTER_NAMES),
        device_name=local_device or _BUILT_IN_DEVICE_NAME,
        host=local_host,
        port=local_port,
    )
