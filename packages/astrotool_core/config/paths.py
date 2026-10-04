"""Where the configuration files live -- the ONE owner of these locations
(#55 D03; CONTRIBUTING.md "Duplicated knowledge").

Two files are read:

- this app's own `~/.CollimationGuideTool/config.toml` (camera panels, mount
  alignment, `[indi]`/`[meridian]`, the filter wheel's INDI identity);
- the sibling SmartTScope project's shared `~/.SmartTScope/config.toml`
  (observer site, optical trains, filter wheel wiring).

plus the local diagnostic-bundle directory `~/.CollimationGuideTool/diagnostics`.

Every reader resolves its default location through `own_config_path()` /
`smarttscope_config_path()` / `diagnostics_dir()` **at call time**, so
redirecting `OWN_CONFIG_PATH` / `SMARTTSCOPE_CONFIG_PATH` / `DIAGNOSTICS_DIR`
here (one monkeypatch each, see tests/conftest.py) redirects every consumer at
once. Older per-module names (`astrotool_core.config.DEFAULT_CONFIG_PATH`,
`astrotool_core.optics.DEFAULT_CONFIG_PATH`,
`astrotool_core.diagnostics.DEFAULT_DIAGNOSTICS_DIR`, ...) remain as
compatibility aliases of the fixed `DEFAULT_*` real locations; patching an
alias redirects nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: This app's directory name under the user's home (also the Pi-side
#: diagnostics location, see `diagnostics.remote`).
APP_DIR_NAME: Final = ".CollimationGuideTool"
SMARTTSCOPE_DIR_NAME: Final = ".SmartTScope"
CONFIG_FILE_NAME: Final = "config.toml"

#: The real locations -- fixed at import, never redirected. Compatibility
#: aliases elsewhere bind to these, so their value never depends on whether a
#: test had redirected the active locations when the alias module was imported.
DEFAULT_OWN_CONFIG_PATH: Final = Path.home() / APP_DIR_NAME / CONFIG_FILE_NAME
DEFAULT_SMARTTSCOPE_CONFIG_PATH: Final = Path.home() / SMARTTSCOPE_DIR_NAME / CONFIG_FILE_NAME
DEFAULT_DIAGNOSTICS_DIR: Final = Path.home() / APP_DIR_NAME / "diagnostics"

#: The active locations, read at call time by every reader -- redirect these.
OWN_CONFIG_PATH: Path = DEFAULT_OWN_CONFIG_PATH
SMARTTSCOPE_CONFIG_PATH: Path = DEFAULT_SMARTTSCOPE_CONFIG_PATH
#: Local diagnostic bundles (`diagnostics.service`).
DIAGNOSTICS_DIR: Path = DEFAULT_DIAGNOSTICS_DIR


def own_config_path() -> Path:
    """`~/.CollimationGuideTool/config.toml`, read at call time."""
    return OWN_CONFIG_PATH


def smarttscope_config_path() -> Path:
    """`~/.SmartTScope/config.toml`, read at call time."""
    return SMARTTSCOPE_CONFIG_PATH


def diagnostics_dir() -> Path:
    """`~/.CollimationGuideTool/diagnostics`, read at call time."""
    return DIAGNOSTICS_DIR
