"""Physical filter wheels and which optical trains use them (issue #41).

One physical wheel is ONE device: one adapter, one connection state, referenced
by every optical train that uses it (today: Main and OAG share one wheel, Guide
has none). Modelling a wheel per train would offer two "independent" Connect
buttons for one piece of hardware and let their states disagree.

    PhysicalFilterWheel  <--  Main
                         <--  OAG
    (Guide: no wheel)

The layout comes from `~/.CollimationGuideTool/config.toml`:

    [filter_wheels.efw1]
    device = "ToupTek EFW 1"      # exact INDI device name (see indi_getprop)
    host = "localhost"            # optional
    port = 7624                   # optional

    [optical_trains.Main]
    filter_wheel = "efw1"
    [optical_trains.OAG]
    filter_wheel = "efw1"
    [optical_trains.Guide]        # no filter_wheel key: this train has no wheel

Without a `[filter_wheels]` table the built-in `DEFAULT_LAYOUT` applies.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.filter_wheel.port import FilterWheelPort

DEFAULT_CONFIG_PATH = Path.home() / ".CollimationGuideTool" / "config.toml"

#: The wheel's device name on this rig's indiserver (`indi_toupcam_wheel`),
#: read with `indi_getprop`; the adapter's own "Filter Wheel" default matches
#: nothing real.
_DEFAULT_DEVICE_NAME = "ToupTek EFW 1"
_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 7624


@dataclass(frozen=True)
class FilterWheelConfig:
    id: str
    device_name: str
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT


@dataclass(frozen=True)
class FilterWheelLayout:
    #: Physical wheels by id, in definition order.
    wheels: tuple[FilterWheelConfig, ...]
    #: (train name, wheel id or None), in definition order.
    trains: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True)
class FilterWheelAssignment:
    """One physical wheel, its (single) port, and the trains that use it."""

    wheel_id: str
    device_name: str
    trains: tuple[str, ...]
    port: FilterWheelPort


DEFAULT_LAYOUT = FilterWheelLayout(
    wheels=(FilterWheelConfig("efw1", _DEFAULT_DEVICE_NAME),),
    trains=(("Main", "efw1"), ("OAG", "efw1"), ("Guide", None)),
)


def _wheel_from(wheel_id: str, table: object) -> FilterWheelConfig | None:
    if not isinstance(table, dict):
        return None
    device = table.get("device")
    if not isinstance(device, str) or not device:
        return None
    host = table.get("host", _DEFAULT_HOST)
    port = table.get("port", _DEFAULT_PORT)
    return FilterWheelConfig(
        id=wheel_id,
        device_name=device,
        host=host if isinstance(host, str) else _DEFAULT_HOST,
        port=port if isinstance(port, int) and not isinstance(port, bool) else _DEFAULT_PORT,
    )


def _train_wheel_id(table: object) -> str | None:
    wheel = table.get("filter_wheel") if isinstance(table, dict) else None
    return wheel if isinstance(wheel, str) and wheel else None


def load_filter_wheel_layout(path: Path | str = DEFAULT_CONFIG_PATH) -> FilterWheelLayout:
    """Read the wheel layout; the built-in default for a missing file/table or
    any malformed input (a convenience override, never required state)."""
    try:
        with Path(path).open("rb") as f:
            data: dict[str, Any] = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return DEFAULT_LAYOUT
    wheel_tables = data.get("filter_wheels")
    if not isinstance(wheel_tables, dict):
        return DEFAULT_LAYOUT
    wheels = tuple(
        wheel
        for wheel_id, table in wheel_tables.items()
        if (wheel := _wheel_from(str(wheel_id), table)) is not None
    )
    if not wheels:
        return DEFAULT_LAYOUT
    train_tables = data.get("optical_trains")
    trains = (
        tuple((str(name), _train_wheel_id(table)) for name, table in train_tables.items())
        if isinstance(train_tables, dict)
        else ()
    )
    return FilterWheelLayout(wheels=wheels, trains=trains)


def default_factory(config: FilterWheelConfig) -> FilterWheelPort:
    return IndiFilterWheelAdapter(config.host, config.port, config.device_name)


def build_filter_wheels(
    layout: FilterWheelLayout,
    factory: Callable[[FilterWheelConfig], FilterWheelPort] = default_factory,
) -> list[FilterWheelAssignment]:
    """One assignment (and ONE adapter) per physical wheel that at least one
    train uses; trains without a wheel, and wheels nobody uses, produce nothing."""
    by_id = {wheel.id: wheel for wheel in layout.wheels}
    users: dict[str, list[str]] = {}
    for train, wheel_id in layout.trains:
        if wheel_id in by_id:
            users.setdefault(wheel_id, []).append(train)
    return [
        FilterWheelAssignment(
            wheel_id=wheel.id,
            device_name=wheel.device_name,
            trains=tuple(users[wheel.id]),
            port=factory(wheel),
        )
        for wheel in layout.wheels
        if wheel.id in users
    ]
