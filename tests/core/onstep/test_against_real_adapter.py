"""The OnStep shims against the REAL OnStepAdapter 0.3.5 -- no hardware.

`FakeOnStepSerial` replaces only `serial.Serial`; `OnStepClient`/`OnStepMount` run unmodified,
so these tests pin what OnStepAdapter itself does with our calls: its safety preconditions
(site config, operator home confirmation), the timed bootstrap at the centering rate, runtime
calibration installs, angular `move_ra`/`move_dec`, and the exact refusal reasons our runner
keys on. A release that changes any of this fails here, not on the rig.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import onstep_adapter.mount as mount_module
import pytest
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.onstep import (
    OnStepConnection,
    OnStepFocuserAdapter,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
    build_onstep_safety_config,
)
from astrotool_core.testing.fake_onstep_serial import FakeOnStepSerial

_SITE_LAT = 50.336
_SITE_LON = 8.533
_CENTER_RATE = 120.0  # arcsec/s the simulated controller really moves at (:RC#)


@pytest.fixture
def sim(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeOnStepSerial]:
    simulator = FakeOnStepSerial(site_lon_deg=_SITE_LON, center_rate_arcsec_per_s=_CENTER_RATE)
    monkeypatch.setattr(mount_module.serial, "Serial", lambda *_a, **_k: simulator)
    yield simulator


def _connection(tmp_path: Path, trust: str = "ntp") -> OnStepConnection:
    home = tmp_path / "home"
    (home / ".SmartTScope").mkdir(parents=True)
    (home / ".SmartTScope" / "config.toml").write_text(
        f"[observer]\nlat = {_SITE_LAT}\nlon = {_SITE_LON}\n"
    )
    config = build_onstep_safety_config(
        home / ".SmartTScope" / "config.toml", tmp_path / "missing-own.toml"
    )
    # keep the adapter's state files inside the test's tmp dir
    import dataclasses

    config = dataclasses.replace(
        config,
        state_file=str(tmp_path / "state.json"),
        mechanical_calibration_file=str(tmp_path / "calibration.json"),
        horizon_path="",
        time_trust_source=trust,
    )
    return OnStepConnection("FAKE", safety_config=config)


class _Rig:
    def __init__(self, tmp_path: Path, trust: str = "ntp") -> None:
        self.connection = _connection(tmp_path, trust)
        self.park = OnStepMountParkAdapter(self.connection)
        self.pulse = OnStepMountPulseAdapter(self.connection)
        self.park.connect()
        self.pulse.connect()

    def unpark_and_confirm_home(self) -> None:
        self.park.unpark()
        self.park.confirm_home()

    def close(self) -> None:
        self.pulse.disconnect()
        self.park.disconnect()


@pytest.fixture
def rig(sim: FakeOnStepSerial, tmp_path: Path) -> Iterator[_Rig]:
    r = _Rig(tmp_path)
    try:
        yield r
    finally:
        r.close()


class TestSafetyPreconditions:
    def test_the_site_and_limits_come_from_the_smarttscope_config(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text("[observer]\nlat = 47.5\nlon = 11.25\n[mount_limits]\nmin_alt_deg = 15.0\n")
        config = build_onstep_safety_config(cfg, tmp_path / "none.toml")
        assert (config.observer_lat, config.observer_lon) == (47.5, 11.25)
        assert config.min_alt_deg == 15.0
        assert config.require_home_confirmation is True

    def test_an_unconfirmed_home_refuses_every_motion_with_the_adapters_reason(
        self, rig: _Rig
    ) -> None:
        rig.park.unpark()  # not confirmed as home by the operator yet
        result = rig.pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300)
        assert not result.accepted
        assert "mechanical_position_authority_untrusted" in result.message

    def test_the_operator_confirmation_is_what_unlocks_motion(self, rig: _Rig) -> None:
        rig.park.unpark()
        assert not rig.park.home_confirmed
        rig.park.confirm_home()
        assert rig.park.home_confirmed
        assert rig.pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300).accepted


class TestAnAstronomyGradeRefusalOnAnUntrustedClock:
    def test_a_merely_plausible_clock_refuses_the_angular_move_but_not_the_manual_bootstrap(
        self, sim: FakeOnStepSerial, tmp_path: Path
    ) -> None:
        rig = _Rig(tmp_path, trust="raspberry_plausible")  # the safe default
        try:
            rig.unpark_and_confirm_home()
            # terrestrial (tracking off) timed manual motion needs no trusted clock ...
            assert rig.pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 400).accepted
            rig.pulse.install_rate(MountAxis.AXIS1, AxisDirection.POSITIVE, 120.0)
            # ... the angular center move does, and says so with the token the runner keys on.
            refused = rig.pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 30.0)
            assert not refused.accepted
            from collimation_tool.ui.mount_test_move_runner import _TIMED_FALLBACK_REFUSALS

            assert any(token in refused.message for token in _TIMED_FALLBACK_REFUSALS)
        finally:
            rig.close()


class TestTimedBootstrap:
    def test_a_terrestrial_bootstrap_moves_the_mount_at_the_centering_rate(
        self, rig: _Rig, sim: FakeOnStepSerial
    ) -> None:
        rig.unpark_and_confirm_home()
        assert not sim.tracking
        result = rig.pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)
        assert result.accepted, result.message
        assert ":RC#" in sim.commands and ":Me#" in sim.commands and ":Qe#" in sim.commands
        assert abs(sim.moved_arcsec["ra"] - _CENTER_RATE * 0.5) < 15.0

    def test_each_axis_direction_drives_its_own_motion(
        self, rig: _Rig, sim: FakeOnStepSerial
    ) -> None:
        rig.unpark_and_confirm_home()
        # Positive first, then back: OnStepAdapter's own limit logic (a counterweight hard
        # limit at the home boundary) refuses to start by going the other way from home.
        for axis, direction, ms, command in [
            (MountAxis.AXIS1, AxisDirection.POSITIVE, 400, ":Me#"),
            (MountAxis.AXIS1, AxisDirection.NEGATIVE, 200, ":Mw#"),
            (MountAxis.AXIS2, AxisDirection.POSITIVE, 400, ":Mn#"),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE, 200, ":Ms#"),
        ]:
            result = rig.pulse.pulse_axis(axis, direction, ms)
            assert result.accepted, result.message
            assert command in sim.commands


class TestRuntimeCalibrationThenAngular:
    def _bootstrap(self, rig: _Rig, sim: FakeOnStepSerial) -> float:
        """Timed bootstrap -> measured rate -> installed; returns the measured rate."""
        rig.unpark_and_confirm_home()
        before = sim.moved_arcsec["ra"]
        assert rig.pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500).accepted
        measured = (sim.moved_arcsec["ra"] - before) / 0.5
        rig.pulse.install_rate(MountAxis.AXIS1, AxisDirection.POSITIVE, measured)
        return measured

    def test_an_angular_move_is_refused_by_the_adapter_until_a_rate_is_installed(
        self, rig: _Rig
    ) -> None:
        rig.unpark_and_confirm_home()
        result = rig.pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 30.0)
        assert not result.accepted
        assert "no centering rate installed" in result.message

    def test_at_home_the_adapter_refuses_the_angular_center_move_with_the_fallback_token(
        self, rig: _Rig
    ) -> None:
        rig.unpark_and_confirm_home()
        rig.pulse.install_rate(MountAxis.AXIS1, AxisDirection.POSITIVE, _CENTER_RATE)
        result = rig.pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 30.0)
        assert not result.accepted
        # the exact reason string MountTestMoveRunner keys its timed fallback on
        from collimation_tool.ui.mount_test_move_runner import _AT_HOME_REFUSAL

        assert _AT_HOME_REFUSAL in result.message

    def test_after_the_bootstrap_an_angular_move_lands_at_the_requested_size(
        self, rig: _Rig, sim: FakeOnStepSerial
    ) -> None:
        measured = self._bootstrap(rig, sim)
        assert abs(measured - _CENTER_RATE) < 30.0
        before = sim.moved_arcsec["ra"]
        result = rig.pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 60.0)
        assert result.accepted, result.message
        assert abs((sim.moved_arcsec["ra"] - before) - 60.0) < 15.0

    def test_the_other_direction_needs_its_own_measured_rate(
        self, rig: _Rig, sim: FakeOnStepSerial
    ) -> None:
        self._bootstrap(rig, sim)
        refused = rig.pulse.move_angular(MountAxis.AXIS1, AxisDirection.NEGATIVE, 30.0)
        assert not refused.accepted and "no centering rate" in refused.message
        assert rig.pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.NEGATIVE, 400).accepted
        rig.pulse.install_rate(MountAxis.AXIS1, AxisDirection.NEGATIVE, _CENTER_RATE)
        before = sim.moved_arcsec["ra"]
        assert rig.pulse.move_angular(MountAxis.AXIS1, AxisDirection.NEGATIVE, 40.0).accepted
        assert abs((sim.moved_arcsec["ra"] - before) + 40.0) < 15.0  # west = negative


class TestParkAndFocuserAgainstTheRealAdapter:
    def test_park_status_and_unpark_leave_tracking_off(
        self, rig: _Rig, sim: FakeOnStepSerial
    ) -> None:
        assert rig.park.status().parked
        rig.park.unpark()
        status = rig.park.status()
        assert not status.parked and not status.tracking

    def test_stop_tracking_is_verified_by_the_adapter(
        self, rig: _Rig, sim: FakeOnStepSerial
    ) -> None:
        rig.park.unpark()
        sim.tracking = True
        rig.park.stop_tracking()
        assert not sim.tracking

    def test_a_controller_without_a_focuser_is_reported_unavailable(self, rig: _Rig) -> None:
        focuser = OnStepFocuserAdapter(rig.connection)
        focuser.connect()
        try:
            assert not focuser.is_available
            assert not focuser.status().available
            assert not focuser.move_absolute(100).accepted
        finally:
            focuser.disconnect()
