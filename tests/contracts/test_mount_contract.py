"""Shared MountPort contract — every mount adapter must satisfy this.

Stage 2 factories: no_mount_factory, fake_mount_factory. indi_mount_factory
(skipif-guarded, real hardware) joins this parametrization in Stage 3.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from astrotool_core.mount import AxisDirection, MountAxis, MountPort, NoMountAdapter
from astrotool_core.testing.fake_mount import FakeMountAdapter

MountFactory = Callable[[], MountPort]


def no_mount_factory() -> MountPort:
    return NoMountAdapter()


def fake_mount_factory() -> MountPort:
    return FakeMountAdapter()


MOUNT_FACTORIES = [no_mount_factory, fake_mount_factory]


@pytest.mark.parametrize("mount_factory", MOUNT_FACTORIES)
def test_capabilities_and_status_are_well_formed(mount_factory: MountFactory) -> None:
    mount = mount_factory()
    mount.connect()
    try:
        caps = mount.capabilities()
        assert caps.min_pulse_ms >= 0
        assert caps.max_pulse_ms >= caps.min_pulse_ms

        status = mount.status()
        assert isinstance(status.connected, bool)
        assert isinstance(status.tracking, bool)
        assert isinstance(status.slewing, bool)
    finally:
        mount.disconnect()


@pytest.mark.parametrize("mount_factory", MOUNT_FACTORIES)
@pytest.mark.parametrize("axis", [MountAxis.AXIS1, MountAxis.AXIS2])
@pytest.mark.parametrize("direction", [AxisDirection.POSITIVE, AxisDirection.NEGATIVE])
def test_pulse_axis_returns_a_command_result(
    mount_factory: MountFactory,
    axis: MountAxis,
    direction: AxisDirection,
) -> None:
    mount = mount_factory()
    mount.connect()
    try:
        result = mount.pulse_axis(axis, direction, 250)
        assert isinstance(result.accepted, bool)
        assert isinstance(result.message, str)
    finally:
        mount.disconnect()


@pytest.mark.parametrize("mount_factory", MOUNT_FACTORIES)
def test_pulse_axis_after_disconnect_is_not_accepted(mount_factory: MountFactory) -> None:
    mount = mount_factory()
    mount.connect()
    mount.disconnect()
    result = mount.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 250)
    assert result.accepted is False


def test_fake_mount_connect_failure_raises_connection_error() -> None:
    mount = FakeMountAdapter(fail_connect=True)
    with pytest.raises(ConnectionError):
        mount.connect()
