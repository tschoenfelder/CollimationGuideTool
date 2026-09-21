"""Where the one OnStep serial port is: the `[onstep]` table of
`~/.CollimationGuideTool/config.toml`, overridable by the `ONSTEP_PORT`
environment variable (the same variable and `/dev/ttyACM0` default
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

DEFAULT_CONFIG_PATH = Path.home() / ".CollimationGuideTool" / "config.toml"
DEFAULT_SERIAL_PORT = "/dev/ttyACM0"
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
