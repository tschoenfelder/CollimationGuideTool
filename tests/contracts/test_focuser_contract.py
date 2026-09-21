"""Shared FocuserPort contract — every focuser adapter must satisfy this.

no_focuser_factory / fake_focuser_factory / onstep_focuser_factory (the
OnStepAdapter shim over a FakeOnStepClient) all run hardware-free.
onstep_real_focuser_factory is real-hardware and skipif-guarded: set
ASTROTOOL_ONSTEP_PORT to exercise it against a real controller.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest
from astrotool_core.focus import FakeFocuser, FocuserPort, NoFocuser
from astrotool_core.onstep import OnStepConnection, OnStepFocuserAdapter
from astrotool_core.testing.fake_onstep_client import make_fake_onstep_connection

FocuserFactory = Callable[[], FocuserPort]

_ONSTEP_PORT = os.environ.get("ASTROTOOL_ONSTEP_PORT")


def no_focuser_factory() -> FocuserPort:
    return NoFocuser()


def fake_focuser_factory() -> FocuserPort:
    return FakeFocuser()


def onstep_focuser_factory() -> FocuserPort:
    return OnStepFocuserAdapter(make_fake_onstep_connection())


def onstep_real_focuser_factory() -> FocuserPort:
    assert _ONSTEP_PORT is not None
    return OnStepFocuserAdapter(OnStepConnection(_ONSTEP_PORT))


FOCUSER_FACTORIES = [no_focuser_factory, fake_focuser_factory, onstep_focuser_factory]
REAL_FOCUSER_FACTORIES = [
    pytest.param(
        onstep_real_focuser_factory,
        marks=pytest.mark.skipif(
            _ONSTEP_PORT is None,
            reason="ASTROTOOL_ONSTEP_PORT not set — no OnStep controller available",
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
