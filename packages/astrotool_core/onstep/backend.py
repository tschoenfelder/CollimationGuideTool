"""Which OnStep the app talks to: the OnStepX mock, the real device, or the real device with
OnStepAdapter's traffic sniffer in between.

Every mode ends in the same place -- ONE `OnStepConnection` holding OnStepAdapter's own
`OnStepIndiClient`, shared by the focuser, park and pulse shims -- and differs only in the
INDI endpoint that client is pointed at (AGENTS.md: OnStepAdapter stays the only route to the
controller in every mode):

- `mock`: an in-process `onstep_mock.SimulatedOnStepServer` (OnStepAdapter's virtual OnStepX
  mount behind a local INDI server) on a free loopback port. No hardware is touched.
- `real`: the configured indiserver (`[indi] host`/`port`), unchanged.
- `real-sniffer` (the default): an `onstep_adapter.capture.CaptureProxy` on a free loopback
  port, forwarding to the configured indiserver. The proxy forwards every byte unchanged and
  never originates one; it records both directions into a daily JSON Lines capture
  (`[onstep] capture_path`, default `~/.CollimationGuideTool/onstep-traffic/
  capture-{date}.jsonl`) that `python -m onstep_adapter.tools.indi_capture check` and
  `python -m onstep_mock.validate` read.

The mode comes from `--onstep MODE` on the command line, else `[onstep] mode` in
`~/.CollimationGuideTool/config.toml`, else the default.
"""

from __future__ import annotations

import logging
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from onstep_adapter import IndiRuntimeConfig

from astrotool_core.config import paths
from astrotool_core.config.device_defaults import INDI_HOST
from astrotool_core.onstep.connection import OnStepConnection

_log = logging.getLogger(__name__)

#: The mock and the sniffer listen on the same numeric loopback the local indiserver uses.
_LOOPBACK = INDI_HOST

#: `{date}` makes OnStepAdapter's writer keep one file per UTC day that every session of that
#: day is appended to (`onstep_adapter.indi_trace.DAY_PLACEHOLDER`).
CAPTURE_FILE_NAME = "capture-{date}.jsonl"


class OnStepMode(StrEnum):
    MOCK = "mock"
    REAL = "real"
    REAL_SNIFFER = "real-sniffer"


DEFAULT_ONSTEP_MODE = OnStepMode.REAL_SNIFFER

#: What a user may type for each mode (config file or command line).
_ALIASES = {
    "mock": OnStepMode.MOCK,
    "real": OnStepMode.REAL,
    "real-sniffer": OnStepMode.REAL_SNIFFER,
    "real_sniffer": OnStepMode.REAL_SNIFFER,
    "sniffer": OnStepMode.REAL_SNIFFER,
}


def parse_onstep_mode(text: str) -> OnStepMode:
    """The mode a user wrote, case and `_`/`-` insensitive; ValueError for anything else."""
    try:
        return _ALIASES[text.strip().lower()]
    except KeyError:
        choices = ", ".join(mode.value for mode in OnStepMode)
        raise ValueError(f"unknown OnStep mode {text!r} (choose {choices})") from None


def _onstep_table(own_path: Path) -> dict[str, object]:
    try:
        with open(own_path, "rb") as f:
            loaded = tomllib.load(f).get("onstep", {})
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def load_onstep_mode(own_path: Path | None = None) -> OnStepMode:
    """`[onstep] mode` from this app's own config; the default when missing or invalid
    (logged), never an error -- the same policy as `load_onstep_indi_config`."""
    value = _onstep_table(own_path or paths.own_config_path()).get("mode")
    if not isinstance(value, str):
        return DEFAULT_ONSTEP_MODE
    try:
        return parse_onstep_mode(value)
    except ValueError as exc:
        _log.warning("%s in [onstep] mode; using %s", exc, DEFAULT_ONSTEP_MODE.value)
        return DEFAULT_ONSTEP_MODE


def load_onstep_capture_path(own_path: Path | None = None) -> Path:
    """`[onstep] capture_path` (`~` expanded; `{date}` keeps one file per UTC day), else
    `capture-{date}.jsonl` in `paths.onstep_capture_dir()`."""
    value = _onstep_table(own_path or paths.own_config_path()).get("capture_path")
    if isinstance(value, str) and value.strip():
        return Path(value.strip()).expanduser()
    return paths.onstep_capture_dir() / CAPTURE_FILE_NAME


class _Service(Protocol):
    def stop(self) -> None: ...


def _start_mock(config: IndiRuntimeConfig) -> tuple[_Service, str, int]:
    from onstep_mock import SimConfig, SimulatedOnStepServer

    server = SimulatedOnStepServer(
        SimConfig(observer_lat=config.observer_lat, observer_lon=config.observer_lon),
        host=_LOOPBACK,
    )
    server.start()
    return server, server.host, server.port


def _start_sniffer(config: IndiRuntimeConfig, capture_path: Path) -> tuple[_Service, str, int]:
    from onstep_adapter.capture import CaptureProxy, open_capture

    capture_path.parent.mkdir(parents=True, exist_ok=True)
    writer = open_capture(str(capture_path), label="CollimationGuideTool")
    proxy = CaptureProxy(
        writer, upstream=(config.host, config.port), listen=(_LOOPBACK, 0), protocol="indi"
    )
    try:
        proxy.start()
    except BaseException:
        writer.recorder.close()
        raise
    return proxy, _LOOPBACK, proxy.port


MockStarter = Callable[[IndiRuntimeConfig], tuple[_Service, str, int]]
SnifferStarter = Callable[[IndiRuntimeConfig, Path], tuple[_Service, str, int]]


@dataclass
class OnStepBackend:
    """The app's one OnStep connection plus whatever local service its mode runs (the mock
    server or the sniffer proxy). `close()` stops that service; the connection itself is
    still opened and closed by its users (`acquire`/`release`)."""

    mode: OnStepMode
    connection: OnStepConnection
    #: Where the sniffer records (`real-sniffer` only).
    capture_path: Path | None = None
    #: The device's own endpoint: the configured indiserver, or the mock's port.
    upstream: tuple[str, int] | None = None
    _service: _Service | None = field(default=None, repr=False)

    @property
    def description(self) -> str:
        """One line for the window title and the log."""
        if self.mode is OnStepMode.MOCK:
            return "OnStep: Mock"
        if self.mode is OnStepMode.REAL_SNIFFER:
            return "OnStep: real device + traffic sniffer"
        return "OnStep: real device"

    def close(self) -> None:
        """Stop the mode's local service (idempotent). The connection's last `release()`
        should come first; a client still open sees its socket closed."""
        service, self._service = self._service, None
        if service is not None:
            service.stop()


def start_onstep_backend(
    mode: OnStepMode,
    config: IndiRuntimeConfig,
    *,
    capture_path: Path | None = None,
    client_factory: Callable[..., Any] | None = None,
    start_mock: MockStarter = _start_mock,
    start_sniffer: SnifferStarter = _start_sniffer,
) -> OnStepBackend:
    """Start `mode`'s local service (if any) and build the connection pointed at it."""

    def connection(cfg: IndiRuntimeConfig) -> OnStepConnection:
        if client_factory is None:
            return OnStepConnection(cfg)
        return OnStepConnection(cfg, client_factory=client_factory)

    if mode is OnStepMode.REAL:
        _log.info("OnStep mode real: indiserver %s:%s", config.host, config.port)
        return OnStepBackend(mode, connection(config), upstream=(config.host, config.port))
    if mode is OnStepMode.MOCK:
        service, host, port = start_mock(config)
        _log.info("OnStep mode mock: OnStepX mock on %s:%s", host, port)
        return OnStepBackend(
            mode,
            connection(replace(config, host=host, port=port)),
            upstream=(host, port),
            _service=service,
        )
    target = capture_path or load_onstep_capture_path()
    service, host, port = start_sniffer(config, target)
    _log.info(
        "OnStep mode real-sniffer: %s:%s -> indiserver %s:%s, recording into %s",
        host, port, config.host, config.port, target,
    )
    return OnStepBackend(
        mode,
        connection(replace(config, host=host, port=port)),
        capture_path=target,
        upstream=(config.host, config.port),
        _service=service,
    )


__all__ = [
    "CAPTURE_FILE_NAME",
    "DEFAULT_ONSTEP_MODE",
    "OnStepBackend",
    "OnStepMode",
    "load_onstep_capture_path",
    "load_onstep_mode",
    "parse_onstep_mode",
    "start_onstep_backend",
]
