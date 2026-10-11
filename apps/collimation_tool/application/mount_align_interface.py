"""Issue #42: the ONE answer to "can Mount Align use the mount right now?" (S6.6a, moved out of
the panel unchanged). Derived fresh from the live ports on every call; no Qt."""

from __future__ import annotations

from dataclasses import dataclass

from astrotool_core.mount import MountParkPort, MountPort


@dataclass(frozen=True)
class MountInterfaceState:
    """Issue #42: the ONE answer to "can Mount Align use the mount right now?".

    Run Calibration, the nudge pad, the screen-move pad and the status text all
    read this -- never separate checks (they used to disagree: the status text
    was only ever SET, so a stale 'not available' message sat beside an enabled
    Run Calibration, field bundle 6dbe7d2c).

    `code`: ok | not_connected | park_interface_unavailable |
    pulse_interface_unavailable."""

    available: bool
    code: str
    detail: str


def mount_interface_state(
    *, connected: bool, mount_park: MountParkPort, mount: MountPort
) -> MountInterfaceState:
    """From the SAME two interfaces calibration actually needs: the park/unpark side (unpark +
    tracking control -- shared with the Mount panel) and the directional pulse side."""
    if not connected:
        return MountInterfaceState(False, "not_connected", "Mount Align is not connected.")
    park_status = mount_park.status()
    if not park_status.available and not park_status.fresh:
        # S6.0c: busy before the first reading -- not "unavailable" (re-review P-c).
        return MountInterfaceState(
            False, "mount_busy", "mount busy / status not read yet (another operation)"
        )
    if not park_status.available:
        return MountInterfaceState(
            False,
            "park_interface_unavailable",
            "the mount park/unpark interface is not available (connect the Mount panel)",
        )
    if not mount.status().connected:
        if getattr(mount, "status_pending", False):  # duck-typed, OnStep adapter
            return MountInterfaceState(
                False, "mount_busy", "mount busy / status not read yet (another operation)"
            )
        return MountInterfaceState(
            False,
            "pulse_interface_unavailable",
            "the mount motion (pulse) interface is not connected",
        )
    return MountInterfaceState(True, "ok", "mount interface available")
