"""Mount port and adapters (no-op, OnStep-backed).

Adapters know how to move an axis; they never decide whether or how
much to move it — that is app-specific policy.
"""

from astrotool_core.mount.no_mount import NoMountAdapter
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountPort,
    MountStatus,
)

__all__ = [
    "AxisDirection",
    "CommandResult",
    "MountAxis",
    "MountCapabilities",
    "MountPort",
    "MountStatus",
    "NoMountAdapter",
]
