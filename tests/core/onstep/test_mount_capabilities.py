"""S6.5 (#55 audit C02): the mount reports its angular-move bounds as capabilities.

OnStepAdapter's own `IndiAxisMover` bound (`onstep_adapter.indi_axis_motion`:
MIN_AXIS_MOVE_ARCSEC / MAX_AXIS_MOVE_DEG) is the single source. Before S6.5 the
floor reached callers only through a duck-typed `min_angular_arcsec` property
(`getattr(mount, "min_angular_arcsec", 0.0)` in the Mount Align panel and the
reacquisition policy) and the ceiling not at all; "can this mount move by angle"
was inferred from three `hasattr` probes. `MountCapabilities` now carries all three;
`min_angular_arcsec` stays as a deprecated alias of the capability.

(The S6.0c held-over `MountStatus.fresh` flag is tested with the threaded adapter tests,
`test_onstep_adapters.py::TestHeldOverMountStatusIsMarked`.)
"""

from __future__ import annotations

import pytest
from astrotool_core.mount import NoMountAdapter
from astrotool_core.mount.port import AxisDirection, MountAxis, MountCapabilities, MountStatus
from astrotool_core.onstep import OnStepMountPulseAdapter
from astrotool_core.testing.fake_onstep_indi_client import make_fake_onstep_indi_connection
from onstep_adapter.indi_axis_motion import MAX_AXIS_MOVE_DEG, MIN_AXIS_MOVE_ARCSEC


def _connected_onstep() -> OnStepMountPulseAdapter:
    connection, made = make_fake_onstep_indi_connection()
    mount = OnStepMountPulseAdapter(connection)
    mount.connect()
    made[0].parked = False
    return mount


class TestOnStepReportsItsAngularBounds:
    def test_the_bounds_come_from_onstep_adapter(self) -> None:
        caps = _connected_onstep().capabilities()
        assert caps.supports_angular_moves is True
        assert caps.min_angular_arcsec == float(MIN_AXIS_MOVE_ARCSEC)
        assert caps.max_angular_arcsec == float(MAX_AXIS_MOVE_DEG) * 3600.0
        assert caps.supports_pulse_guiding is False  # S6.0: unchanged

    def test_the_bounds_are_reported_before_connect_too(self) -> None:
        """A capability of the installed OnStepAdapter, not of a connection."""
        caps = OnStepMountPulseAdapter(make_fake_onstep_indi_connection()[0]).capabilities()
        assert (caps.supports_angular_moves, caps.min_angular_arcsec) == (
            True,
            float(MIN_AXIS_MOVE_ARCSEC),
        )

    def test_the_deprecated_alias_returns_the_capability(self) -> None:
        mount = _connected_onstep()
        assert mount.min_angular_arcsec == mount.capabilities().min_angular_arcsec

    @pytest.mark.parametrize("axis", [MountAxis.AXIS1, MountAxis.AXIS2])
    def test_move_angular_accepts_exactly_the_reported_range(self, axis: MountAxis) -> None:
        mount = _connected_onstep()
        caps = mount.capabilities()
        low, high = caps.min_angular_arcsec, caps.max_angular_arcsec
        assert high is not None
        for arcsec, accepted in (
            (low, True),
            (high, True),
            (low * 0.99, False),
            (high * 1.01, False),
        ):
            result = mount.move_angular(axis, AxisDirection.POSITIVE, arcsec)
            assert result.accepted is accepted, (arcsec, result.message)


class TestCapabilityDefaults:
    def test_a_mount_without_angular_moves_reports_no_bounds(self) -> None:
        caps = NoMountAdapter().capabilities()
        assert (caps.supports_angular_moves, caps.min_angular_arcsec, caps.max_angular_arcsec) == (
            False,
            0.0,
            None,
        )

    def test_existing_constructors_keep_working(self) -> None:
        caps = MountCapabilities(supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9)
        assert (caps.supports_angular_moves, caps.min_angular_arcsec, caps.max_angular_arcsec) == (
            False,
            0.0,
            None,
        )

    @pytest.mark.parametrize(
        ("low", "high"), [(-1.0, None), (10.0, 5.0), (float("nan"), None), (0.0, float("inf"))]
    )
    def test_inconsistent_bounds_are_rejected(self, low: float, high: float | None) -> None:
        with pytest.raises(ValueError):
            MountCapabilities(
                supports_pulse_guiding=False,
                min_pulse_ms=0,
                max_pulse_ms=0,
                supports_angular_moves=True,
                min_angular_arcsec=low,
                max_angular_arcsec=high,
            )

    def test_a_status_is_fresh_unless_marked(self) -> None:
        assert MountStatus(connected=True, tracking=True, slewing=False).fresh is True
