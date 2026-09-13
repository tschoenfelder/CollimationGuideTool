"""Shared FilterWheelPort contract — every filter wheel adapter must
satisfy this. Issue #34: read-only status display, no commanding.

no_filter_wheel_factory / fake_filter_wheel_factory /
indi_filter_wheel_factory (against a real, in-process FakeIndiServer —
see astrotool_core.testing) all run hardware-free.
indi_real_filter_wheel_factory is real-hardware and skipif-guarded,
mirroring tests/contracts/test_focuser_contract.py's own
indi_focuser_factory/ASTROTOOL_INDI_FOCUSER_HOST pattern — set
ASTROTOOL_INDI_FILTER_WHEEL_HOST (and optionally
ASTROTOOL_INDI_FILTER_WHEEL_PORT/ASTROTOOL_INDI_FILTER_WHEEL_DEVICE) to
exercise it against a real indiserver.
"""

from __future__ import annotations

import os
import weakref
from collections.abc import Callable

import pytest
from astrotool_core.filter_wheel import FakeFilterWheel, FilterWheelPort, NoFilterWheel
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.testing.fake_indi_server import FakeIndiServer

FilterWheelFactory = Callable[[], FilterWheelPort]

_INDI_FILTER_WHEEL_HOST = os.environ.get("ASTROTOOL_INDI_FILTER_WHEEL_HOST")
_INDI_FILTER_WHEEL_PORT = int(os.environ.get("ASTROTOOL_INDI_FILTER_WHEEL_PORT", "7624"))
_INDI_FILTER_WHEEL_DEVICE = os.environ.get("ASTROTOOL_INDI_FILTER_WHEEL_DEVICE", "Filter Wheel")


def no_filter_wheel_factory() -> FilterWheelPort:
    return NoFilterWheel()


def fake_filter_wheel_factory() -> FilterWheelPort:
    return FakeFilterWheel()


def indi_filter_wheel_factory() -> FilterWheelPort:
    # FakeIndiServer's lifetime is tied to the adapter's via weakref.finalize
    # -- same convention as test_focuser_contract.py's own indi_focuser_factory.
    fake = FakeIndiServer(
        device_name=_INDI_FILTER_WHEEL_DEVICE,
        filter_names=("Luminance", "Red", "Green", "Blue", "OIII"),
    )
    fake.start()
    adapter = IndiFilterWheelAdapter(fake.host, fake.port, connect_timeout_s=2.0)
    weakref.finalize(adapter, fake.stop)
    return adapter


def indi_real_filter_wheel_factory() -> FilterWheelPort:
    assert _INDI_FILTER_WHEEL_HOST is not None
    return IndiFilterWheelAdapter(
        _INDI_FILTER_WHEEL_HOST, _INDI_FILTER_WHEEL_PORT, _INDI_FILTER_WHEEL_DEVICE
    )


FILTER_WHEEL_FACTORIES = [
    no_filter_wheel_factory,
    fake_filter_wheel_factory,
    indi_filter_wheel_factory,
]
REAL_FILTER_WHEEL_FACTORIES = [
    pytest.param(
        indi_real_filter_wheel_factory,
        marks=pytest.mark.skipif(
            _INDI_FILTER_WHEEL_HOST is None,
            reason="ASTROTOOL_INDI_FILTER_WHEEL_HOST not set — no real indiserver available",
        ),
    ),
]


@pytest.mark.parametrize("filter_wheel_factory", FILTER_WHEEL_FACTORIES)
def test_status_matches_is_available(filter_wheel_factory: FilterWheelFactory) -> None:
    filter_wheel = filter_wheel_factory()
    filter_wheel.connect()
    try:
        status = filter_wheel.status()
        assert status.available == filter_wheel.is_available
        assert isinstance(status.moving, bool)
    finally:
        filter_wheel.disconnect()


@pytest.mark.parametrize("filter_wheel_factory", FILTER_WHEEL_FACTORIES)
def test_unavailable_status_never_reports_a_slot(filter_wheel_factory: FilterWheelFactory) -> None:
    filter_wheel = filter_wheel_factory()
    status = filter_wheel.status()  # never connected
    if not status.available:
        assert status.current_slot is None
        assert status.reason is not None


def test_no_filter_wheel_is_never_available() -> None:
    filter_wheel = NoFilterWheel()
    assert filter_wheel.is_available is False
    status = filter_wheel.status()
    assert status.available is False
    assert status.current_slot is None
    assert status.reason is not None


def test_fake_filter_wheel_reports_configured_state() -> None:
    filter_wheel = FakeFilterWheel(slot=3, filter_name="OIII", moving=True)
    filter_wheel.connect()
    status = filter_wheel.status()
    assert status.available is True
    assert status.current_slot == 3
    assert status.filter_name == "OIII"
    assert status.moving is True


def test_fake_filter_wheel_connect_failure_raises_connection_error() -> None:
    filter_wheel = FakeFilterWheel(fail_connect=True)
    with pytest.raises(ConnectionError):
        filter_wheel.connect()


@pytest.mark.parametrize("filter_wheel_factory", REAL_FILTER_WHEEL_FACTORIES)
def test_real_indi_filter_wheel_connects_and_is_available(
    filter_wheel_factory: FilterWheelFactory,
) -> None:
    filter_wheel = filter_wheel_factory()
    filter_wheel.connect()
    try:
        assert filter_wheel.is_available is True
        status = filter_wheel.status()
        assert status.available is True
        assert status.current_slot is not None
    finally:
        filter_wheel.disconnect()
