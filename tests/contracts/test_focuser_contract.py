"""Shared FocuserPort contract — every focuser adapter must satisfy this.

no_focuser_factory / fake_focuser_factory / indi_focuser_factory (against
a real, in-process FakeIndiServer — see astrotool_core.testing) all run
hardware-free. indi_real_focuser_factory is real-hardware and
skipif-guarded, mirroring tests/contracts/test_mount_contract.py's
indi_mount_factory/ASTROTOOL_ONSTEP_PORT pattern — set
ASTROTOOL_INDI_FOCUSER_HOST (and optionally ASTROTOOL_INDI_FOCUSER_PORT/
ASTROTOOL_INDI_FOCUSER_DEVICE) to exercise it against a real indiserver.
"""

from __future__ import annotations

import os
import weakref
from collections.abc import Callable

import pytest
from astrotool_core.focus import FakeFocuser, FocuserPort, NoFocuser
from astrotool_core.focus.indi_focuser_adapter import IndiFocuserAdapter
from astrotool_core.testing.fake_indi_server import FakeIndiServer

FocuserFactory = Callable[[], FocuserPort]

_INDI_FOCUSER_HOST = os.environ.get("ASTROTOOL_INDI_FOCUSER_HOST")
_INDI_FOCUSER_PORT = int(os.environ.get("ASTROTOOL_INDI_FOCUSER_PORT", "7624"))
_INDI_FOCUSER_DEVICE = os.environ.get("ASTROTOOL_INDI_FOCUSER_DEVICE", "LX200 OnStep")


def no_focuser_factory() -> FocuserPort:
    return NoFocuser()


def fake_focuser_factory() -> FocuserPort:
    return FakeFocuser()


def indi_focuser_factory() -> FocuserPort:
    # FakeIndiServer's lifetime is tied to the adapter's via weakref.finalize
    # (rather than threading it through every test's try/finally) since the
    # factory signature here is a plain `Callable[[], FocuserPort]` with no
    # separate teardown hook, matching the shape every other factory in this
    # file already has.
    fake = FakeIndiServer()
    fake.start()
    adapter = IndiFocuserAdapter(fake.host, fake.port, connect_timeout_s=2.0)
    weakref.finalize(adapter, fake.stop)
    return adapter


def indi_real_focuser_factory() -> FocuserPort:
    assert _INDI_FOCUSER_HOST is not None
    return IndiFocuserAdapter(_INDI_FOCUSER_HOST, _INDI_FOCUSER_PORT, _INDI_FOCUSER_DEVICE)


FOCUSER_FACTORIES = [no_focuser_factory, fake_focuser_factory, indi_focuser_factory]
REAL_FOCUSER_FACTORIES = [
    pytest.param(
        indi_real_focuser_factory,
        marks=pytest.mark.skipif(
            _INDI_FOCUSER_HOST is None,
            reason="ASTROTOOL_INDI_FOCUSER_HOST not set — no real indiserver available",
        ),
    ),
]


@pytest.mark.parametrize("focuser_factory", FOCUSER_FACTORIES)
def test_status_matches_is_available_and_get_position(focuser_factory: FocuserFactory) -> None:
    focuser = focuser_factory()
    focuser.connect()
    try:
        status = focuser.status()
        assert status.available == focuser.is_available
        assert status.max_position == focuser.get_max_position()
        assert isinstance(status.moving, bool)
    finally:
        focuser.disconnect()


@pytest.mark.parametrize("focuser_factory", FOCUSER_FACTORIES)
def test_move_absolute_reports_a_result(focuser_factory: FocuserFactory) -> None:
    focuser = focuser_factory()
    result = focuser.move_absolute(100)
    assert isinstance(result.accepted, bool)
    assert result.target_position == 100


@pytest.mark.parametrize("focuser_factory", FOCUSER_FACTORIES)
def test_stop_and_is_moving_are_safe_to_call(focuser_factory: FocuserFactory) -> None:
    focuser = focuser_factory()
    focuser.stop()  # must not raise
    assert isinstance(focuser.is_moving(), bool)


def test_fake_focuser_move_absolute_updates_position() -> None:
    focuser = FakeFocuser()
    focuser.move_absolute(1234)
    assert focuser.get_position() == 1234


def test_fake_focuser_connect_failure_raises_connection_error() -> None:
    focuser = FakeFocuser(fail_connect=True)
    with pytest.raises(ConnectionError):
        focuser.connect()


def test_no_focuser_is_never_available() -> None:
    focuser = NoFocuser()
    assert focuser.is_available is False


@pytest.mark.parametrize("focuser_factory", REAL_FOCUSER_FACTORIES)
def test_real_indi_focuser_connects_and_is_available(focuser_factory: FocuserFactory) -> None:
    focuser = focuser_factory()
    focuser.connect()
    try:
        assert focuser.is_available is True
        status = focuser.status()
        assert status.available is True
        assert status.max_position > 0
    finally:
        focuser.disconnect()
