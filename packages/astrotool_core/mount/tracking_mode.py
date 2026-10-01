"""Tracking-mode verification — issue #30's "Tracking is not slewing":
star calibration requires tracking ON, terrestrial calibration requires
tracking OFF, and the wrong mode is an invalid precondition, not
something calibration should silently tolerate or guess about.

Deliberately its own tiny module, not folded into `park_port.py` or the
acquisition layer: this is a distinct physical-state concern (issue #30's
own "Fundamental state distinction" section) from both park/unpark
lifecycle management and frame-timing/stability verification, even though
all three cooperate during a calibration sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.timing import SYSTEM_CLOCK, Clock, Deadline


class TrackingMode(Enum):
    ON = "on"
    OFF = "off"


class TrackingVerificationStatus(Enum):
    #: The mount was already in the required mode -- no correction issued.
    ALREADY_CORRECT = "already_correct"
    #: The mount was in the wrong mode; a correction was sent and the
    #: mount now reports the required mode.
    REPAIRED = "repaired"
    #: A correction was sent, but the mount still doesn't report the
    #: required mode (e.g. a real mount that refuses to track while
    #: parked) -- issue #30 #8: "unexpected tracking-state change" and
    #: any other mismatch must invalidate the current measurement rather
    #: than proceeding on a wrong assumption.
    REPAIR_FAILED = "repair_failed"
    #: The mount isn't connected/available at all -- nothing to verify.
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class TrackingVerificationResult:
    status: TrackingVerificationStatus
    observed_mode: TrackingMode | None  # None only for UNAVAILABLE

    @property
    def ok(self) -> bool:
        return self.status in (
            TrackingVerificationStatus.ALREADY_CORRECT, TrackingVerificationStatus.REPAIRED,
        )


def _mode_of(tracking: bool) -> TrackingMode:
    return TrackingMode.ON if tracking else TrackingMode.OFF


def ensure_tracking_mode(
    mount_park: MountParkPort,
    required: TrackingMode,
    *,
    settle_timeout_s: float = 0.0,
    poll_interval_s: float = 0.05,
    clock: Clock | None = None,
) -> TrackingVerificationResult:
    """Verifies `mount_park`'s current tracking state matches `required`,
    issuing `start_tracking()`/`stop_tracking()` and re-checking if not —
    issue #30: "The stable-frame layer shall verify or establish the
    required tracking mode before calibration measurements begin" and
    "After each commanded slew/pulse, tracking state shall be
    re-verified" (both call sites use this same function; a caller
    invokes it once before the BEFORE capture and again after every
    commanded movement — see `mount_test_move_panel` wiring).

    Deliberately synchronous/no retry loop beyond the one correction
    attempt — a mount that doesn't accept the correction (wrong mode
    persists) reports `REPAIR_FAILED` rather than looping indefinitely;
    the caller decides whether/how to bound further attempts, matching
    this project's existing pattern of bounded, explicit failure over a
    silent or unbounded wait (see `MountTestMoveRunner`'s own pulse-
    rejection retries for the analogous existing convention).

    The optional settle poll reads time and waits through `clock` (issue
    #53; default: the real clock), so tests drive it with a `FakeClock`.
    """
    status = mount_park.status()
    if not status.available:
        return TrackingVerificationResult(TrackingVerificationStatus.UNAVAILABLE, None)

    current = _mode_of(status.tracking)
    if current is required:
        return TrackingVerificationResult(TrackingVerificationStatus.ALREADY_CORRECT, current)

    if required is TrackingMode.ON:
        mount_park.start_tracking()
    else:
        mount_park.stop_tracking()

    # Issue #44: a real (INDI) driver applies the command asynchronously, so
    # the immediate re-read may still show the old state -- optionally poll
    # (bounded) for the mount to confirm before declaring the repair failed.
    # Same order as before the clock was injected (issue #53): immediate
    # re-read, then the deadline is taken, then poll.
    source = clock or SYSTEM_CLOCK
    repaired_mode = _mode_of(mount_park.status().tracking)
    deadline = Deadline.after(settle_timeout_s, clock=source)
    while repaired_mode is not required and not deadline.expired():
        source.sleep(poll_interval_s)
        repaired_mode = _mode_of(mount_park.status().tracking)
    if repaired_mode is required:
        return TrackingVerificationResult(TrackingVerificationStatus.REPAIRED, repaired_mode)
    return TrackingVerificationResult(TrackingVerificationStatus.REPAIR_FAILED, repaired_mode)
