"""Optical-train geometry: plate scale, field of view. Currently just the
SmartTScope config.toml reader — see smarttscope_config's docstring.
"""

from astrotool_core.optics.smarttscope_config import (
    DEFAULT_CONFIG_PATH,
    KNOWN_PIXEL_SIZE_UM,
    load_pixel_scale_arcsec,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "KNOWN_PIXEL_SIZE_UM",
    "load_pixel_scale_arcsec",
]
