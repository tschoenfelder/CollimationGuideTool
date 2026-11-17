"""Say clearly when OnStep itself has stopped answering (field incident 908a26b9).

A USB over-current reset dropped the mount's serial adapter while indiserver and the
OnStep INDI driver kept running: the driver polled `:GU#` once a second and got no
answer, the published position sat in "Alert" without changing, and OnStepAdapter refused
the unpark with its generic "Fresh parked, stationary and fault-free status is required" --
true, but it does not say what is wrong or what to do. This module turns that state into one
crisp message. Neutral: it reads only the snapshot fields OnStepAdapter already publishes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from astrotool_core.onstep.errors import ONSTEP_OPERATION_ERRORS

_log = logging.getLogger(__name__)

#: OnStepAdapter snapshot blockers that mean "no fresh answer from the controller".
_NO_ANSWER_BLOCKERS = frozenset({"onstep_status_not_fresh", "onstep_status_unavailable"})
_DISCONNECTED_BLOCKER = "indi_device_disconnected"


def reconnect_hint(device: str) -> str:
    """What to do, for the INDI device name the connection uses."""
    return (
        f'Reconnect "{device}" in Ekos, or on the Pi run '
        f'`indi_setprop "{device}.CONNECTION.DISCONNECT=On"` and then '
        f'`indi_setprop "{device}.CONNECTION.CONNECT=On"`; if that does not bring it back, '
        "restart indiserver. Usual cause: the mount's USB serial link dropped "
        "(check `dmesg -T | grep -i -E \"over-current|ttyUSB\"`)."
    )


class _Snapshot(Protocol):
    blockers: tuple[str, ...]
    status_age_ms: float | None


class _Mount(Protocol):
    def get_status(self) -> _Snapshot: ...


def onstep_not_answering(snapshot: _Snapshot, device: str) -> str | None:
    """The crisp reason when the controller is not answering, else None."""
    blockers = set(snapshot.blockers)
    if _DISCONNECTED_BLOCKER in blockers:
        return f"OnStep is not connected in INDI -- nothing was sent. {reconnect_hint(device)}"
    if blockers & _NO_ANSWER_BLOCKERS:
        age = snapshot.status_age_ms
        since = f" for {age / 1000.0:.0f} s" if age is not None and age >= 1000.0 else ""
        return (
            f"OnStep is not answering{since}: the INDI driver gets no status from the mount "
            f"-- nothing was sent. {reconnect_hint(device)}"
        )
    return None


def explain_failure(mount: _Mount, detail: str, device: str) -> str:
    """`detail` (OnStepAdapter's own reason), led by the crisp "not answering" reason when a
    fresh reading shows the controller is silent. Never raises: a failing read keeps `detail`."""
    try:
        reason = onstep_not_answering(mount.get_status(), device)
    except Exception:  # noqa: BLE001 -- explaining a failure must not mask it
        return detail
    return detail if reason is None else f"{reason} (OnStepAdapter: {detail})"


@dataclass(frozen=True)
class Recovery:
    """What an automatic reconnect did (`recover_if_silent`)."""

    attempted: bool
    recovered: bool
    #: For the operator: what happened and why (None when nothing was attempted).
    notice: str | None


_NOTHING = Recovery(attempted=False, recovered=False, notice=None)


def recover_if_silent(mount: object, device: str, action: str) -> Recovery:
    """USER DECISION (2026-11-17, field incident 908a26b9): when `action` failed because
    OnStep had stopped answering, reconnect the INDI driver automatically (OnStepAdapter
    >= 0.6.14 `reconnect_device`: DISCONNECT + CONNECT, never moves the mount) -- and log the
    reason, so the operator is aware. Does nothing when the controller answers, when the
    installed OnStepAdapter has no reconnect, or when the status cannot be read."""
    get_status = getattr(mount, "get_status", None)
    reconnect = getattr(mount, "reconnect_device", None)
    if not callable(get_status) or not callable(reconnect):
        return _NOTHING
    try:
        snapshot = get_status()
        reason = onstep_not_answering(snapshot, device)
    except Exception:  # noqa: BLE001 -- a failing read must not mask the original failure
        return _NOTHING
    if reason is None:
        return _NOTHING
    age = getattr(snapshot, "status_age_ms", None)
    silent = f"{age / 1000.0:.0f} s" if age else "unknown time"
    _log.warning(
        '%s failed because OnStep is not answering (no status for %s, blockers %s): '
        'reconnecting the INDI driver "%s" automatically',
        action, silent, sorted(set(snapshot.blockers)), device,
    )
    try:
        result = reconnect()
    except ONSTEP_OPERATION_ERRORS as exc:
        _log.error('automatic reconnect of "%s" failed: %s', device, exc)
        return Recovery(True, False, f"the automatic reconnect of the INDI driver failed: {exc}")
    if not getattr(result, "recovered", False):
        error = getattr(result, "error", None)
        _log.error('automatic reconnect of "%s" did not bring OnStep back: %s', device, error)
        return Recovery(
            True, False, f"the automatic reconnect did not bring OnStep back ({error})"
        )
    elapsed = float(getattr(result, "elapsed_s", 0.0))
    _log.warning(
        'INDI driver "%s" reconnected after %.1f s: OnStep answers again (%s)',
        device, elapsed, action,
    )
    return Recovery(
        True,
        True,
        f'OnStep had stopped answering (no status for {silent}); the app reconnected the INDI '
        f'driver "{device}" automatically ({elapsed:.0f} s). Usual cause: the mount\'s USB '
        "serial link dropped -- check the USB power (dmesg: over-current).",
    )


__all__ = [
    "Recovery",
    "explain_failure",
    "onstep_not_answering",
    "reconnect_hint",
    "recover_if_silent",
]
