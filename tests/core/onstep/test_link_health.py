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
from astrotool_core.testing.fake_onstep_indi_client import (
    FakeOnStepIndiClient,
    make_fake_onstep_indi_connection,
)

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
    made[0].reconnect_recovers = False  # the automatic reconnect (below) does not help here
    with pytest.raises(RuntimeError) as raised:
        park.unpark()
    message = str(raised.value)
    assert message.startswith(
        "OnStep did not reach unparked with tracking off: OnStep is not answering for 30 s"
    )
    assert 'Reconnect "' in message and 'CONNECTION.CONNECT=On' in message


def _silent_park_adapter(
    *, recovers: bool = True
) -> tuple[OnStepMountParkAdapter, FakeOnStepIndiClient]:
    conn, made = make_fake_onstep_indi_connection()
    park = OnStepMountParkAdapter(conn)
    park.connect()
    made[0].snapshot_blockers = ("onstep_status_not_fresh",)
    made[0].status_age_ms = 40_000.0
    made[0].reconnect_recovers = recovers
    return park, made[0]


def test_user_decision_a_silent_controller_is_reconnected_and_the_unpark_retried(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """USER DECISION 2026-11-17: reconnect automatically, and log the reason."""
    park, client = _silent_park_adapter()
    with caplog.at_level("WARNING", logger="astrotool_core.onstep.link_health"):
        park.unpark()
    assert client.reconnect_calls == 1
    assert not client.parked  # the retried unpark went through
    logged = caplog.text
    assert "unpark failed because OnStep is not answering (no status for 40 s" in logged
    assert "reconnecting the INDI driver" in logged and "reconnected after 2.5 s" in logged
    notice = park.take_notice()
    assert notice is not None and notice.startswith("OnStep had stopped answering")
    assert "reconnected the INDI driver" in notice
    assert park.take_notice() is None  # shown once


def test_a_reconnect_that_does_not_help_is_reported_and_not_looped() -> None:
    park, client = _silent_park_adapter(recovers=False)
    with pytest.raises(RuntimeError) as raised:
        park.unpark()
    assert client.reconnect_calls == 1
    message = str(raised.value)
    assert "OnStep is not answering for 40 s" in message
    assert "the automatic reconnect did not bring OnStep back" in message


def test_a_healthy_controller_is_never_reconnected() -> None:
    conn, made = make_fake_onstep_indi_connection()
    park = OnStepMountParkAdapter(conn)
    park.connect()
    made[0].unpark_rejected = True
    with pytest.raises(RuntimeError, match="fake rejected unpark"):
        park.unpark()
    assert made[0].reconnect_calls == 0
    assert park.take_notice() is None


def test_a_failed_move_reconnects_but_is_never_repeated() -> None:
    from astrotool_core.mount.port import AxisDirection, MountAxis
    from astrotool_core.onstep import OnStepMountPulseAdapter

    conn, made = make_fake_onstep_indi_connection()
    pulse = OnStepMountPulseAdapter(conn)
    pulse.connect()
    client = made[0]
    client.parked = False
    client.tracking = True  # the fake's move refuses (as a silent controller would)
    client.snapshot_blockers = ("onstep_status_not_fresh",)
    client.status_age_ms = 9_000.0
    result = pulse.move_angular(MountAxis.AXIS2, AxisDirection.POSITIVE, 600.0)
    assert not result.accepted
    assert client.reconnect_calls == 1 and client.axis_move_calls == []
    assert "reconnected the INDI driver" in result.message
    assert "The move was not repeated -- click again." in result.message
