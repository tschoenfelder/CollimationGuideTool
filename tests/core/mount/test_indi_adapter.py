"""Unit tests for IndiMountAdapter's no-hardware paths: construction (no
real port needed — OnStepClient defers opening the serial connection to
connect()), capabilities(), and the not-connected branches of status()/
pulse_axis(). The connected branches require a real OnStep mount and are
exercised by the skipif-guarded real-hardware contract test instead.
"""

from __future__ import annotations

from astrotool_core.mount.indi_adapter import IndiMountAdapter
from astrotool_core.mount.port import AxisDirection, MountAxis


def test_construction_does_not_require_a_real_port() -> None:
    adapter = IndiMountAdapter("COM99")
    assert adapter is not None


def test_capabilities_reports_pulse_guiding_support() -> None:
    adapter = IndiMountAdapter("COM99")
    caps = adapter.capabilities()
    assert caps.supports_pulse_guiding is True
    assert caps.min_pulse_ms == 1
    assert caps.max_pulse_ms == 9999


def test_status_when_not_connected() -> None:
    adapter = IndiMountAdapter("COM99")
    status = adapter.status()
    assert status.connected is False
    assert status.tracking is False
    assert status.slewing is False


def test_pulse_axis_when_not_connected_is_not_accepted() -> None:
    adapter = IndiMountAdapter("COM99")
    result = adapter.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 250)
    assert result.accepted is False
    assert result.message == "not connected"
