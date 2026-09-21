"""Shared MountParkPort contract — every park adapter must satisfy this.

no_mount_park_factory / fake_mount_park_factory / onstep_mount_park_factory
(the OnStepAdapter shim over a FakeOnStepClient) run hardware-free;
onstep_real_mount_park_factory is skipif-guarded on ASTROTOOL_ONSTEP_PORT.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

import pytest
from astrotool_core.mount.no_mount_park import NoMountPark
from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.onstep import OnStepConnection, OnStepMountParkAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.fake_onstep_client import make_fake_onstep_connection

MountParkFactory = Callable[[], MountParkPort]

_ONSTEP_PORT = os.environ.get("ASTROTOOL_ONSTEP_PORT")


def no_mount_park_factory() -> MountParkPort:
    return NoMountPark()


def fake_mount_park_factory() -> MountParkPort:
    return FakeMountPark()


def onstep_mount_park_factory() -> MountParkPort:
    return OnStepMountParkAdapter(make_fake_onstep_connection())


def onstep_real_mount_park_factory() -> MountParkPort:
    assert _ONSTEP_PORT is not None
    return OnStepMountParkAdapter(OnStepConnection(_ONSTEP_PORT))


MOUNT_PARK_FACTORIES = [no_mount_park_factory, fake_mount_park_factory, onstep_mount_park_factory]
REAL_MOUNT_PARK_FACTORIES = [
    pytest.param(
        onstep_real_mount_park_factory,
        marks=pytest.mark.skipif(
            _ONSTEP_PORT is None,
            reason="ASTROTOOL_ONSTEP_PORT not set — no OnStep controller available",
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


@pytest.mark.parametrize("mount_park_factory", MOUNT_PARK_FACTORIES)
def test_stop_tracking_is_safe_to_call_before_connect(
    mount_park_factory: MountParkFactory,
) -> None:
    mount = mount_park_factory()
    mount.stop_tracking()  # must not raise


@pytest.mark.parametrize("mount_park_factory", MOUNT_PARK_FACTORIES)
def test_stop_tracking_deactivates_tracking(mount_park_factory: MountParkFactory) -> None:
    mount = mount_park_factory()
    mount.connect()
    try:
        mount.unpark()
        # Waited, not asserted immediately -- the real INDI factory settles
        # tracking asynchronously over its socket round-trip (unlike
        # FakeMountPark/NoMountPark, which settle synchronously and satisfy
        # this on the first check).
        _wait_until(lambda: mount.status().tracking is False)
        mount.stop_tracking()
        _wait_until(lambda: mount.status().tracking is False)
        assert mount.status().tracking is False
    finally:
        mount.disconnect()


def _wait_until(
    predicate: Callable[[], bool], timeout_s: float = 2.0, message: str = "condition never met"
) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, message
        time.sleep(0.01)


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
def test_real_onstep_mount_park_connects_and_is_available(
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
