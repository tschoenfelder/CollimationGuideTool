"""Shared FocuserPort contract — every focuser adapter must satisfy this.

Stage 2 factories: no_focuser_factory, fake_focuser_factory.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from astrotool_core.focus import FakeFocuser, FocuserPort, NoFocuser

FocuserFactory = Callable[[], FocuserPort]


def no_focuser_factory() -> FocuserPort:
    return NoFocuser()


def fake_focuser_factory() -> FocuserPort:
    return FakeFocuser()


FOCUSER_FACTORIES = [no_focuser_factory, fake_focuser_factory]


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
