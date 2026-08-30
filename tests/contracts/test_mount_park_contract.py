"""Shared MountParkPort contract — every park adapter must satisfy this.

no_mount_park_factory / fake_mount_park_factory / indi_mount_park_factory
(against a real, in-process FakeIndiServer) all run hardware-free.
indi_real_mount_park_factory is real-hardware and skipif-guarded,
mirroring test_mount_contract.py's indi_mount_factory/
ASTROTOOL_ONSTEP_PORT pattern — set ASTROTOOL_INDI_MOUNT_HOST (and
optionally ASTROTOOL_INDI_MOUNT_PORT/ASTROTOOL_INDI_MOUNT_DEVICE) to
exercise it against a real indiserver.
"""

from __future__ import annotations

import os
import weakref
from collections.abc import Callable

import pytest
from astrotool_core.mount.indi_mount_park_adapter import IndiMountParkAdapter
from astrotool_core.mount.no_mount_park import NoMountPark
from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.testing.fake_indi_server import FakeIndiServer
from astrotool_core.testing.fake_mount_park import FakeMountPark

MountParkFactory = Callable[[], MountParkPort]

_INDI_MOUNT_HOST = os.environ.get("ASTROTOOL_INDI_MOUNT_HOST")
_INDI_MOUNT_PORT = int(os.environ.get("ASTROTOOL_INDI_MOUNT_PORT", "7624"))
_INDI_MOUNT_DEVICE = os.environ.get("ASTROTOOL_INDI_MOUNT_DEVICE", "LX200 OnStep")


def no_mount_park_factory() -> MountParkPort:
    return NoMountPark()


def fake_mount_park_factory() -> MountParkPort:
    return FakeMountPark()


def indi_mount_park_factory() -> MountParkPort:
    # FakeIndiServer's lifetime is tied to the adapter's via weakref.finalize
    # — see test_focuser_contract.py's indi_focuser_factory for why.
    fake = FakeIndiServer()
    fake.start()
    adapter = IndiMountParkAdapter(fake.host, fake.port, connect_timeout_s=2.0)
    weakref.finalize(adapter, fake.stop)
    return adapter


def indi_real_mount_park_factory() -> MountParkPort:
    assert _INDI_MOUNT_HOST is not None
    return IndiMountParkAdapter(_INDI_MOUNT_HOST, _INDI_MOUNT_PORT, _INDI_MOUNT_DEVICE)


MOUNT_PARK_FACTORIES = [no_mount_park_factory, fake_mount_park_factory, indi_mount_park_factory]
REAL_MOUNT_PARK_FACTORIES = [
    pytest.param(
        indi_real_mount_park_factory,
        marks=pytest.mark.skipif(
            _INDI_MOUNT_HOST is None,
            reason="ASTROTOOL_INDI_MOUNT_HOST not set — no real indiserver available",
        ),
    ),
]


@pytest.mark.parametrize("mount_park_factory", MOUNT_PARK_FACTORIES)
def test_status_matches_is_available(mount_park_factory: MountParkFactory) -> None:
    mount = mount_park_factory()
    mount.connect()
    try:
        status = mount.status()
        assert status.available == mount.is_available
        assert isinstance(status.parked, bool)
        assert isinstance(status.tracking, bool)
    finally:
        mount.disconnect()


@pytest.mark.parametrize("mount_park_factory", MOUNT_PARK_FACTORIES)
def test_park_and_unpark_are_safe_to_call_before_connect(
    mount_park_factory: MountParkFactory,
) -> None:
    mount = mount_park_factory()
    mount.park()  # must not raise
    mount.unpark()  # must not raise


def test_fake_mount_park_connect_failure_raises_connection_error() -> None:
    mount = FakeMountPark(fail_connect=True)
    with pytest.raises(ConnectionError):
        mount.connect()


def test_fake_mount_park_unpark_deactivates_tracking() -> None:
    mount = FakeMountPark()
    mount.connect()
    mount._tracking = True  # noqa: SLF001 -- simulate tracking already on
    mount.unpark()
    assert mount.status().tracking is False


def test_no_mount_park_is_never_available() -> None:
    mount = NoMountPark()
    assert mount.is_available is False


@pytest.mark.parametrize("mount_park_factory", REAL_MOUNT_PARK_FACTORIES)
def test_real_indi_mount_park_connects_and_is_available(
    mount_park_factory: MountParkFactory,
) -> None:
    mount = mount_park_factory()
    mount.connect()
    try:
        assert mount.is_available is True
        status = mount.status()
        assert status.available is True
    finally:
        mount.disconnect()
