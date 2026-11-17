"""Field incident 908a26b9: a USB reset left the INDI OnStep driver without answers from the
controller, and unpark failed with OnStepAdapter's generic "Fresh parked, stationary and
fault-free status is required". The app must say crisply that OnStep is not answering and how
to reconnect."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from astrotool_core.onstep import OnStepMountParkAdapter
from astrotool_core.onstep.link_health import (
    explain_failure,
    onstep_not_answering,
    reconnect_hint,
)
from astrotool_core.testing.fake_onstep_indi_client import make_fake_onstep_indi_connection

pytestmark = pytest.mark.unit

_GENERIC = "Fresh parked, stationary and fault-free status is required"
_DEVICE = "Mount Device"


def _snapshot(*blockers: str, age_ms: float | None = 0.0) -> SimpleNamespace:
    return SimpleNamespace(blockers=blockers, status_age_ms=age_ms)


def test_a_silent_controller_is_named_with_how_long_and_the_fix() -> None:
    reason = onstep_not_answering(_snapshot("onstep_status_not_fresh", age_ms=12_400.0), _DEVICE)
    assert reason is not None
    assert reason.startswith("OnStep is not answering for 12 s")
    assert "nothing was sent" in reason and reconnect_hint(_DEVICE) in reason
    assert f"{_DEVICE}.CONNECTION.DISCONNECT=On" in reason and "over-current" in reason


def test_a_disconnected_driver_is_named_too() -> None:
    reason = onstep_not_answering(_snapshot("indi_device_disconnected"), _DEVICE)
    assert reason is not None and reason.startswith("OnStep is not connected in INDI")


def test_a_healthy_or_otherwise_blocked_controller_keeps_the_adapters_reason() -> None:
    assert onstep_not_answering(_snapshot(), _DEVICE) is None
    assert onstep_not_answering(_snapshot("pier_side_unknown"), _DEVICE) is None
    healthy = SimpleNamespace(get_status=lambda: _snapshot())
    assert explain_failure(healthy, _GENERIC, _DEVICE) == _GENERIC


def test_explaining_never_masks_the_failure_when_the_read_itself_fails() -> None:
    def broken() -> SimpleNamespace:
        raise OSError("socket closed")

    assert explain_failure(SimpleNamespace(get_status=broken), _GENERIC, _DEVICE) == _GENERIC


def test_unpark_on_a_silent_controller_says_so_first() -> None:
    conn, made = make_fake_onstep_indi_connection()
    park = OnStepMountParkAdapter(conn)
    park.connect()
    made[0].unpark_rejected = True
    made[0].snapshot_blockers = ("onstep_status_not_fresh",)
    made[0].status_age_ms = 30_000.0
    with pytest.raises(RuntimeError) as raised:
        park.unpark()
    message = str(raised.value)
    assert message.startswith(
        "OnStep did not reach unparked with tracking off: OnStep is not answering for 30 s"
    )
    assert 'Reconnect "' in message and 'CONNECTION.CONNECT=On' in message
