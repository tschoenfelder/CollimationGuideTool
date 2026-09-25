"""Physical filter wheels and which optical trains use them (issue #41; #47
made the wiring source itself correct rather than invented -- see
`filter_wheel.config`'s own docstring).

One physical wheel is ONE device: one adapter, one connection state, referenced
by every optical train that uses it. Modelling a wheel per train would offer
two "independent" Connect buttons for one piece of hardware and let their
states disagree.

    PhysicalFilterWheel  <--  <whichever train the shared config names>

The wiring (which train currently uses the wheel, and its per-slot filter
names) comes from `filter_wheel.config.load_filter_wheel_wiring` -- the
shared `~/.SmartTScope/config.toml` first, then the same table shapes in
`~/.CollimationGuideTool/config.toml`, then a built-in default matching this
rig's known-good state (verified live: the wheel is in Main's optical path
only, not shared with OAG as an earlier version of this module assumed).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from astrotool_core.filter_wheel.config import (
    DEFAULT_LOCAL_CONFIG_PATH,
    DEFAULT_SMARTTSCOPE_CONFIG_PATH,
    load_filter_wheel_wiring,
)
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.filter_wheel.port import FilterWheelPort

#: Only ever used when neither config source names a host/port -- matches
#: this rig's real indiserver (see filter_wheel.config's own built-in default).
_DEFAULT_HOST = "localhost"
_DEFAULT_PORT = 7624
_WHEEL_ID = "efw1"


@dataclass(frozen=True)
class FilterWheelConfig:
    id: str
    device_name: str
    host: str = _DEFAULT_HOST
    port: int = _DEFAULT_PORT
    #: slot -> short filter code (e.g. {2: "R"}) -- issue #47; used as a
    #: fallback when the device itself reports no name for a slot.
    filter_names: dict[int, str] = field(default_factory=dict)
    #: True -> `filter_names` beat the names the INDI driver reports.
    names_override: bool = False


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


def load_filter_wheel_layout(
    *,
    smarttscope_path: Path | str = DEFAULT_SMARTTSCOPE_CONFIG_PATH,
    local_path: Path | str = DEFAULT_LOCAL_CONFIG_PATH,
) -> FilterWheelLayout:
    """One wheel, wired to whichever train `load_filter_wheel_wiring` names --
    an empty layout (no wheel, no trains) when the wiring is explicitly
    disabled or names no train."""
    wiring = load_filter_wheel_wiring(smarttscope_path=smarttscope_path, local_path=local_path)
    if not wiring.enabled or wiring.active_train is None:
        return FilterWheelLayout(wheels=(), trains=())
    wheel = FilterWheelConfig(
        id=_WHEEL_ID,
        # load_filter_wheel_wiring() always resolves device_name to a real
        # string whenever enabled=True (its own built-in default, if
        # nothing else names one) -- this `or` is an unreachable-in-
        # practice safety net, so it must still name a real device rather
        # than a stale duplicate of that same default.
        device_name=wiring.device_name or "ToupTek EFW 2",
        host=wiring.host or _DEFAULT_HOST,
        port=wiring.port or _DEFAULT_PORT,
        filter_names=dict(wiring.filter_names),
        names_override=wiring.names_override,
    )
    return FilterWheelLayout(wheels=(wheel,), trains=((wiring.active_train, _WHEEL_ID),))


def default_factory(config: FilterWheelConfig) -> FilterWheelPort:
    return IndiFilterWheelAdapter(
        config.host,
        config.port,
        config.device_name,
        filter_names=config.filter_names,
        names_override=config.names_override,
    )


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
