"""Say clearly when OnStep itself has stopped answering (field incident 908a26b9).

A USB over-current reset dropped the mount's serial adapter while indiserver and the
OnStep INDI driver kept running: the driver polled `:GU#` once a second and got no
answer, the published position sat in "Alert" without changing, and OnStepAdapter refused
the unpark with its generic "Fresh parked, stationary and fault-free status is required" --
true, but it does not say what is wrong or what to do. This module turns that state into one
crisp message. Neutral: it reads only the snapshot fields OnStepAdapter already publishes.
"""

from __future__ import annotations

from typing import Protocol

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


__all__ = ["explain_failure", "onstep_not_answering", "reconnect_hint"]
