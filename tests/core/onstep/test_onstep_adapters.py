"""The OnStep shims over OnStepAdapter (AGENTS.md: the ONLY OnStep connection).

Faked at OnStepAdapter's own public surface (`FakeOnStepClient`), so these pin
what this app asks OnStepAdapter to do and how it reports the answer.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import cast

import pytest
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.onstep import (
    OnStepConnection,
    OnStepFocuserAdapter,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
    load_onstep_settings,
)
from astrotool_core.testing.fake_onstep_client import FakeOnStepClient
from onstep_adapter import OnStepClient
from onstep_adapter.ports.mount import MountState


def _connection() -> tuple[OnStepConnection, list[FakeOnStepClient]]:
    made: list[FakeOnStepClient] = []

    def factory(port: str, **kwargs: float) -> OnStepClient:
        client = FakeOnStepClient(port, **kwargs)  # type: ignore[arg-type]
        made.append(client)
        return cast(OnStepClient, client)

    return OnStepConnection("/dev/ttyTEST", client_factory=factory), made


class TestOneSharedClient:
    def test_all_shims_share_a_single_client_and_port(self) -> None:
        conn, made = _connection()
        OnStepFocuserAdapter(conn).connect()
        OnStepMountParkAdapter(conn).connect()
        OnStepMountPulseAdapter(conn).connect()
        assert len(made) == 1
        assert made[0].port == "/dev/ttyTEST"
        assert made[0].connect_calls == 1

    def test_port_is_closed_only_when_the_last_shim_disconnects(self) -> None:
        conn, made = _connection()
        focuser, park = OnStepFocuserAdapter(conn), OnStepMountParkAdapter(conn)
        focuser.connect()
        park.connect()
        focuser.disconnect()
        assert not made[0].closed
        park.disconnect()
        assert made[0].closed

    def test_disconnect_is_idempotent_and_never_closes_someone_elses_hold(self) -> None:
        conn, made = _connection()
        a, b = OnStepMountParkAdapter(conn), OnStepFocuserAdapter(conn)
        a.connect()
        b.connect()
        a.disconnect()
        a.disconnect()
        assert not made[0].closed

    def test_failed_open_raises_and_leaves_nothing_held(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)

        def failing(port: str, **kwargs: float) -> OnStepClient:
            c = FakeOnStepClient(port, **kwargs)  # type: ignore[arg-type]
            c.connect_ok = False
            made.append(c)
            return cast(OnStepClient, c)

        bad = OnStepConnection("/dev/ttyBAD", client_factory=failing)
        with pytest.raises(ConnectionError):
            OnStepMountParkAdapter(bad).connect()
        assert made[-1].closed
        assert bad.client is None
        park.connect()  # an unrelated, healthy connection is unaffected
        assert park.is_available


class TestPark:
    def test_status_reflects_the_adapters_state(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        assert park.status().parked and not park.status().tracking
        made[0].mount.state = MountState.TRACKING
        assert park.status().tracking and not park.status().parked

    def test_unpark_uses_the_adapters_verified_unpark_with_tracking_off(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        park.unpark()
        assert made[0].mount.calls == ["recovery_unpark_stop_tracking"]
        assert not park.status().parked and not park.status().tracking

    def test_rejected_unpark_is_an_error_not_silence(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].mount.unpark_rejected = True
        with pytest.raises(RuntimeError):
            park.unpark()

    def test_stop_tracking_that_does_not_take_is_an_error(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].mount.state = MountState.TRACKING
        made[0].mount.tracking_sticks_on = True
        with pytest.raises(RuntimeError):
            park.stop_tracking()

    def test_operations_are_noops_when_not_connected(self) -> None:
        park = OnStepMountParkAdapter(_connection()[0])
        assert not park.is_available
        assert not park.status().available
        park.unpark()
        park.stop_tracking()
        park.start_tracking()
        park.park()


class TestFocuser:
    def test_moves_and_status_go_through_the_adapter(self) -> None:
        conn, made = _connection()
        f = OnStepFocuserAdapter(conn)
        f.connect()
        assert f.move_absolute(500).accepted
        assert f.get_position() == 500
        f.move(-100)
        assert f.status().position == 400
        f.stop()
        assert made[0].focuser.stop_calls == 1

    def test_rejected_move_is_reported_not_claimed(self) -> None:
        conn, made = _connection()
        f = OnStepFocuserAdapter(conn)
        f.connect()
        made[0].focuser.reject_moves = True
        assert not f.move_absolute(500).accepted

    def test_controller_without_a_focuser_reports_unavailable(self) -> None:
        conn, made = _connection()
        f = OnStepFocuserAdapter(conn)
        f.connect()
        made[0].focuser.available = False
        assert not f.is_available
        assert not f.status().available
        assert not f.move_absolute(10).accepted


class TestPulse:
    def _ready(self, tracking: bool = False) -> tuple[OnStepMountPulseAdapter, FakeOnStepClient]:
        conn, made = _connection()
        pulse = OnStepMountPulseAdapter(conn)
        pulse.connect()
        made[0].mount.state = MountState.TRACKING if tracking else MountState.UNPARKED
        made[0].mount.at_home = not tracking
        return pulse, made[0]

    def test_axis_and_direction_map_to_adapter_timed_moves(self) -> None:
        pulse, client = self._ready()
        for axis, direction in [
            (MountAxis.AXIS1, AxisDirection.POSITIVE),
            (MountAxis.AXIS1, AxisDirection.NEGATIVE),
            (MountAxis.AXIS2, AxisDirection.POSITIVE),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE),
        ]:
            assert pulse.pulse_axis(axis, direction, 500, rate_preset="6").accepted
        calls = client.mount.motion_calls
        got = [(c.axis, c.direction, c.duration_ms, c.rate_preset) for c in calls]
        assert got == [
            ("ra", "east", 500, 6),
            ("ra", "west", 500, 6),
            ("dec", "north", 500, 6),
            ("dec", "south", 500, 6),
        ]

    def test_tracking_off_uses_manual_mode_tracking_on_uses_center(self) -> None:
        pulse, client = self._ready(tracking=False)
        pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300)
        assert client.mount.motion_calls[-1].mode == "manual"
        pulse2, client2 = self._ready(tracking=True)
        pulse2.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300)
        assert client2.mount.motion_calls[-1].mode == "center"

    def test_parked_mount_is_rejected_with_the_adapters_reason(self) -> None:
        conn, made = _connection()
        pulse = OnStepMountPulseAdapter(conn)
        pulse.connect()
        result = pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300)
        assert not result.accepted
        assert "mount_parked" in result.message

    def test_out_of_range_duration_is_rejected_not_raised(self) -> None:
        pulse, _ = self._ready()
        assert not pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 5).accepted

    def test_bad_preset_is_rejected(self) -> None:
        pulse, _ = self._ready()
        result = pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300, rate_preset="x")
        assert not result.accepted

    def test_not_connected_is_rejected(self) -> None:
        pulse = OnStepMountPulseAdapter(_connection()[0])
        assert not pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300).accepted

    def test_abort_cancels_a_running_pulse_promptly(self) -> None:
        pulse, client = self._ready()
        client.mount.simulate_motion_s = 5.0
        out: list[bool] = []

        def run() -> None:
            out.append(pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 4000).accepted)

        t = threading.Thread(target=run)
        started = time.monotonic()
        t.start()
        time.sleep(0.1)
        pulse.abort()
        t.join(2.0)
        assert not t.is_alive()
        assert out == [False]
        assert time.monotonic() - started < 2.0
        assert client.mount.stop_calls == 1

    def test_a_pulse_after_an_abort_runs_normally(self) -> None:
        pulse, _ = self._ready()
        pulse.abort()
        assert pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300).accepted


class TestSettings:
    def test_defaults_when_nothing_is_configured(self, tmp_path: Path) -> None:
        s = load_onstep_settings(tmp_path / "missing.toml", environ={})
        assert (s.serial_port, s.baud_rate) == ("/dev/ttyACM0", 9600)

    def test_config_file_then_environment_override(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[onstep]\nserial_port = "/dev/ttyUSB1"\nbaud_rate = 57600\n')
        assert load_onstep_settings(cfg, environ={}).serial_port == "/dev/ttyUSB1"
        assert load_onstep_settings(cfg, environ={}).baud_rate == 57600
        env = {"ONSTEP_PORT": "/dev/ttyX"}
        assert load_onstep_settings(cfg, environ=env).serial_port == "/dev/ttyX"

    def test_malformed_values_fall_back_to_defaults(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text('[onstep]\nserial_port = 5\nbaud_rate = "fast"\n')
        s = load_onstep_settings(cfg, environ={})
        assert (s.serial_port, s.baud_rate) == ("/dev/ttyACM0", 9600)
