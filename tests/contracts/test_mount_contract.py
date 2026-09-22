"""Shared MountPort contract — every mount adapter must satisfy this.

no_mount_factory / fake_mount_factory / onstep_pulse_mount_factory (the
OnStepAdapter shim over a FakeOnStepIndiClient) run hardware-free.
onstep_real_mount_factory is real-hardware and skipif-guarded — no INDI
server is present in this Windows dev environment (set
ASTROTOOL_ONSTEP_INDI to exercise it against a real one).
"""

from __future__ import annotations

import os
from collections.abc import Callable

import pytest
from astrotool_core.mount import AxisDirection, MountAxis, MountPort, NoMountAdapter
from astrotool_core.onstep import OnStepConnection, OnStepMountPulseAdapter, load_onstep_indi_config
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_onstep_indi_client import make_fake_onstep_indi_connection

MountFactory = Callable[[], MountPort]

_ONSTEP_INDI = os.environ.get("ASTROTOOL_ONSTEP_INDI")


def no_mount_factory() -> MountPort:
    return NoMountAdapter()


def fake_mount_factory() -> MountPort:
    return FakeMountAdapter()


def onstep_pulse_mount_factory() -> MountPort:
    return OnStepMountPulseAdapter(make_fake_onstep_indi_connection()[0])


def onstep_real_mount_factory() -> MountPort:
    assert _ONSTEP_INDI is not None
    return OnStepMountPulseAdapter(OnStepConnection(load_onstep_indi_config()))


MOUNT_FACTORIES = [no_mount_factory, fake_mount_factory, onstep_pulse_mount_factory]
REAL_MOUNT_FACTORIES = [
    pytest.param(
        onstep_real_mount_factory,
        marks=pytest.mark.skipif(
            _ONSTEP_INDI is None,
            reason="ASTROTOOL_ONSTEP_INDI not set — no OnStep INDI server available",
        ),
    ),
]


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
def test_pulse_axis_accepts_an_explicit_rate_preset(mount_factory: MountFactory) -> None:
    """rate_preset is optional on every MountPort implementation -- calling
    with an explicit value must not raise or change whether the pulse is
    accepted, regardless of whether the adapter actually does anything with
    it (OnStepMountPulseAdapter passes it on to OnStepAdapter)."""
    mount = mount_factory()
    mount.connect()
    try:
        result = mount.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 250, rate_preset="7")
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


@pytest.mark.parametrize("mount_factory", REAL_MOUNT_FACTORIES)
def test_real_onstep_mount_capabilities_and_status(mount_factory: MountFactory) -> None:
    mount = mount_factory()
    mount.connect()
    try:
        caps = mount.capabilities()
        # No timed/rate-based pulse exists over INDI (>= 0.4.0) -- see
        # OnStepMountPulseAdapter's module docstring.
        assert caps.supports_pulse_guiding is False
        status = mount.status()
        assert status.connected is True
    finally:
        mount.disconnect()
