"""The OnStep shims over OnStepAdapter (AGENTS.md: the ONLY OnStep connection).

Faked at OnStepAdapter's own public surface (`FakeOnStepIndiClient`), so
these pin what this app asks OnStepAdapter to do and how it reports the
answer. OnStepAdapter >= 0.4.0 is INDI-backed (AGENTS.md: for this
deployment, indiserver owns the OnStep serial port and OnStepAdapter itself
talks INDI, so IndiMonitor keeps working alongside this app) -- there is no
serial port/baud rate to configure any more. Tracking-enable and a usable
angular-move floor (originally 720", now 30" after OnStepAdapter#14) are
both real now; 0.3.5's timed pulse (a duration-at-a-rate move) still has
no INDI equivalent -- see each adapter's own module docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from astrotool_core.mount.port import AxisDirection, MountAxis
from astrotool_core.onstep import (
    OnStepConnection,
    OnStepFocuserAdapter,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
    load_onstep_indi_config,
)
from astrotool_core.testing.fake_onstep_indi_client import (
    FakeOnStepIndiClient,
    fake_indi_runtime_config,
    make_fake_onstep_indi_connection,
)


def _connection() -> tuple[OnStepConnection, list[FakeOnStepIndiClient]]:
    return make_fake_onstep_indi_connection()


class TestOneSharedClient:
    def test_all_shims_share_a_single_client(self) -> None:
        conn, made = _connection()
        OnStepFocuserAdapter(conn).connect()
        OnStepMountParkAdapter(conn).connect()
        OnStepMountPulseAdapter(conn).connect()
        assert len(made) == 1
        assert made[0].connect_calls == 1

    def test_connection_is_closed_only_when_the_last_shim_disconnects(self) -> None:
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

        made_bad: list[FakeOnStepIndiClient] = []

        def failing(**kwargs: object) -> FakeOnStepIndiClient:
            c = FakeOnStepIndiClient(config=fake_indi_runtime_config())
            c.connect_ok = False
            made_bad.append(c)
            return c

        bad = OnStepConnection(fake_indi_runtime_config(), client_factory=failing)
        with pytest.raises(ConnectionError):
            OnStepMountParkAdapter(bad).connect()
        assert made_bad[-1].closed
        assert bad.client is None
        park.connect()  # an unrelated, healthy connection is unaffected
        assert park.is_available


class TestPark:
    def test_status_reflects_the_adapters_state(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        assert park.status().parked and not park.status().tracking
        made[0].parked, made[0].tracking = False, True
        assert park.status().tracking and not park.status().parked

    def test_unpark_is_a_switch_flip_not_a_slew(self) -> None:
        """`unpark()` never requests HOME (`indi_home.py`'s own docstring)
        -- it must not claim `at_home` afterward."""
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].at_home = False
        park.unpark()
        assert not park.status().parked and not park.status().tracking
        assert not made[0].at_home

    def test_park_goes_via_the_adapters_own_route(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        park.unpark()
        park.park()
        assert park.status().parked

    def test_the_long_running_actions_are_flagged_for_the_ui(self) -> None:
        assert OnStepMountParkAdapter.long_running_actions is True

    def test_rejected_unpark_is_an_error_not_silence(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].unpark_rejected = True
        with pytest.raises(RuntimeError):
            park.unpark()

    def test_park_and_go_home_are_gated_by_home_motion_enabled(self) -> None:
        """OnStepAdapter itself refuses park/unpark/go_home until
        `[indi].home_motion_enabled = true` -- pending its own supervised
        HOME-status check. The refusal surfaces as a plain `RuntimeError`,
        same as any other rejected park/unpark."""
        conn, made = make_fake_onstep_indi_connection(
            fake_indi_runtime_config(home_motion_enabled=False)
        )
        park = OnStepMountParkAdapter(conn)
        park.connect()
        with pytest.raises(RuntimeError, match="HOME/PARK motion is disabled"):
            park.unpark()
        with pytest.raises(RuntimeError, match="HOME/PARK motion is disabled"):
            park.park()

    def test_stop_tracking_routes_to_emergency_stop_and_is_verified(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].tracking = True
        park.stop_tracking()
        assert not made[0].tracking

    def test_stop_tracking_that_does_not_take_is_an_error(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].tracking = True
        made[0].stop_confirms = False
        with pytest.raises(RuntimeError):
            park.stop_tracking()

    def test_start_tracking_enables_tracking_when_safe(self) -> None:
        """OnStepAdapter#14: tracking-enable is real now, not always
        `NotImplementedError` (issue #30's star-mode calibration needs
        this)."""
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        park.unpark()
        made[0].at_home = False  # OnStepAdapter's own preflight refuses at home too
        park.start_tracking()
        assert made[0].tracking is True

    def test_start_tracking_from_an_unsafe_state_is_an_error(self) -> None:
        """Still parked -- OnStepAdapter's own preflight refuses; the
        refusal surfaces as `RuntimeError`, not silence."""
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        with pytest.raises(RuntimeError, match="did not confirm tracking on"):
            park.start_tracking()

    def test_start_tracking_that_does_not_confirm_is_an_error(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        park.unpark()
        made[0].at_home = False
        made[0].tracking_rejected = True
        with pytest.raises(RuntimeError, match="did not confirm tracking on"):
            park.start_tracking()

    def test_park_needs_no_home_only_stationary_and_not_tracking(self) -> None:
        """OnStepAdapter#16 (0.4.1): a plain unpark -> park cycle works
        without ever visiting HOME."""
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        park.unpark()
        assert not made[0].at_home
        park.park()
        assert park.status().parked

    def test_park_while_tracking_is_still_refused(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].parked, made[0].tracking = False, True
        with pytest.raises(RuntimeError, match="non-tracking"):
            park.park()

    def test_unpark_of_an_already_unparked_mount_succeeds(self) -> None:
        """OnStepAdapter#17 (0.4.1): tracking already off / not parked no
        longer times out waiting for a fresh push."""
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].parked = False
        park.unpark()
        assert not park.status().parked and not park.status().tracking

    def test_strict_tracking_policy_still_needs_home_authority(self) -> None:
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        park.unpark()
        made[0].home_authority_established = False  # what this app always has
        with pytest.raises(RuntimeError, match="did not confirm tracking on"):
            park.start_tracking()

    def test_controller_managed_policy_starts_tracking_without_home(self) -> None:
        conn, made = make_fake_onstep_indi_connection(
            fake_indi_runtime_config(tracking_authority_policy="controller_managed")
        )
        park = OnStepMountParkAdapter(conn)
        park.connect()
        park.unpark()
        made[0].home_authority_established = False
        made[0].time_site_authority = False
        park.start_tracking()
        assert made[0].tracking is True

    def test_no_confirm_home_method_and_home_confirmed_is_automatic(self) -> None:
        """Unlike 0.3.5, home authority is established automatically from
        live status -- there is no manual operator confirmation call."""
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        assert not hasattr(park, "confirm_home")
        park.connect()
        assert park.home_confirmed is True  # fake default
        made[0].home_authority_established = False
        assert park.home_confirmed is False

    def test_operations_are_noops_when_not_connected(self) -> None:
        park = OnStepMountParkAdapter(_connection()[0])
        assert not park.is_available
        assert not park.status().available
        park.unpark()
        park.stop_tracking()
        park.start_tracking()
        park.park()
        assert park.home_confirmed is False


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
        made[0].focuser.connected = False
        assert not f.is_available
        assert not f.status().available
        assert not f.move_absolute(10).accepted

    def test_operations_are_noops_when_not_connected(self) -> None:
        f = OnStepFocuserAdapter(_connection()[0])
        assert not f.is_available
        assert not f.status().available
        assert f.get_position() == 0
        assert f.get_max_position() == 0
        assert not f.is_moving()
        f.move(10)
        f.stop()


class TestPulse:
    def _ready(
        self, *, tracking: bool = False
    ) -> tuple[OnStepMountPulseAdapter, FakeOnStepIndiClient]:
        conn, made = _connection()
        pulse = OnStepMountPulseAdapter(conn)
        pulse.connect()
        made[0].parked = False
        made[0].tracking = tracking
        return pulse, made[0]

    def test_capabilities_report_no_timed_pulse_support(self) -> None:
        pulse, _ = self._ready()
        caps = pulse.capabilities()
        assert caps.supports_pulse_guiding is False
        assert (caps.min_pulse_ms, caps.max_pulse_ms) == (0, 0)

    def test_pulse_axis_always_refuses_no_indi_primitive_exists(self) -> None:
        pulse, client = self._ready()
        result = pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 500)
        assert not result.accepted
        assert "no timed pulse primitive" in result.message
        assert client.axis_move_calls == []

    def test_status_reflects_the_adapters_live_tracking_and_slewing(self) -> None:
        pulse, client = self._ready()
        assert not pulse.status().tracking
        client.tracking = True
        assert pulse.status().tracking

    def test_not_connected_is_rejected(self) -> None:
        pulse = OnStepMountPulseAdapter(_connection()[0])
        assert not pulse.pulse_axis(MountAxis.AXIS1, AxisDirection.POSITIVE, 300).accepted
        assert not pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 1000.0).accepted
        assert pulse.status() == pulse.status()  # safe, does not raise


class TestAngularMotion:
    """>= this dev build of 0.4.0: `move_angular` maps to
    `IndiMount.move_ra_axis_deg`/`move_dec_axis_deg`, a finite,
    feedback-verified GOTO -- bounded to 30"-36000" (OnStepAdapter's own
    range, raised from an original 720" floor per OnStepAdapter#14), no
    rate installation needed."""

    def _ready(
        self, *, tracking: bool = False
    ) -> tuple[OnStepMountPulseAdapter, FakeOnStepIndiClient]:
        conn, made = _connection()
        pulse = OnStepMountPulseAdapter(conn)
        pulse.connect()
        made[0].parked = False
        made[0].tracking = tracking
        return pulse, made[0]

    def test_a_move_below_the_floor_is_refused_not_silently_clamped(self) -> None:
        pulse, client = self._ready()
        result = pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 10.0)
        assert not result.accepted
        assert "outside OnStepAdapter's supported axis-move range" in result.message
        assert client.axis_move_calls == []

    def test_a_move_at_or_above_the_floor_is_accepted(self) -> None:
        """AGENTS.md's own Mount Align seeds (195"-1595") all clear this
        floor now (raised from an original 720" -- OnStepAdapter#14)."""
        pulse, client = self._ready()
        assert pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 30.0).accepted
        assert pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 195.0).accepted

    def test_a_move_above_the_ceiling_is_refused(self) -> None:
        pulse, client = self._ready()
        result = pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 40000.0)
        assert not result.accepted
        assert client.axis_move_calls == []

    def test_install_rate_is_accepted_but_never_required(self) -> None:
        """Unlike 0.3.5, `move_angular` needs no installed rate at all --
        `install_rate`/`installed_rate` are kept only for API compatibility."""
        pulse, client = self._ready()
        assert pulse.installed_rate(MountAxis.AXIS1, AxisDirection.POSITIVE) is None
        pulse.install_rate(MountAxis.AXIS1, AxisDirection.POSITIVE, 120.0)
        assert pulse.installed_rate(MountAxis.AXIS1, AxisDirection.POSITIVE) == 120.0
        # ... yet a move works before any rate is installed for the OTHER direction:
        assert pulse.move_angular(MountAxis.AXIS1, AxisDirection.NEGATIVE, 1000.0).accepted

    def test_invalid_rates_are_still_rejected(self) -> None:
        pulse, _ = self._ready()
        for bad in (0.0, -3.0, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                pulse.install_rate(MountAxis.AXIS1, AxisDirection.POSITIVE, bad)

    def test_angular_moves_map_to_signed_degree_offsets(self) -> None:
        pulse, client = self._ready()
        for axis, direction in [
            (MountAxis.AXIS1, AxisDirection.POSITIVE),
            (MountAxis.AXIS1, AxisDirection.NEGATIVE),
            (MountAxis.AXIS2, AxisDirection.POSITIVE),
            (MountAxis.AXIS2, AxisDirection.NEGATIVE),
        ]:
            assert pulse.move_angular(axis, direction, 1000.0).accepted
        got = client.axis_move_calls
        assert got == [
            ("ra", 1000.0 / 3600.0),
            ("ra", -1000.0 / 3600.0),
            ("dec", 1000.0 / 3600.0),
            ("dec", -1000.0 / 3600.0),
        ]

    def test_tracking_on_is_refused_with_the_adapters_own_reason(self) -> None:
        """OnStepAdapter's own precondition (axis motion requires tracking
        already off) -- this shim does not re-implement the check, it just
        surfaces OnStepAdapter's refusal."""
        pulse, client = self._ready(tracking=True)
        result = pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 1000.0)
        assert not result.accepted
        assert "non-tracking" in result.message

    def test_a_bad_size_is_rejected(self) -> None:
        pulse, _ = self._ready()
        assert not pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 0.0).accepted
        bad = pulse.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, float("nan"))
        assert not bad.accepted


class TestSettings:
    def test_defaults_when_nothing_is_configured(self, tmp_path: Path) -> None:
        config = load_onstep_indi_config(tmp_path / "missing.toml", tmp_path / "missing2.toml")
        assert config.host == "127.0.0.1"
        assert config.port == 7624
        assert config.device == "LX200 OnStep"
        assert config.home_motion_enabled is False
        assert config.focuser_max_position is None

    def test_default_meridian_values_clear_a_tight_real_rig_guard(self, tmp_path: Path) -> None:
        """Real-rig regression (rasppi3): an earlier 100.0/110.0 default
        could never satisfy OnStepAdapter's own `derive_meridian_policy`
        against a real, much tighter firmware guard (measured ~2.0 degrees
        there) -- it refused to connect at all. These defaults must clear
        a guard at least that tight."""
        config = load_onstep_indi_config(tmp_path / "missing.toml", tmp_path / "missing2.toml")
        measured_guard_deg = min(12.0, 8.0) / 4.0  # rasppi3's real firmware readback
        assert 0 < config.flip_request_deg < config.tracking_stop_deg < measured_guard_deg

    def test_observer_site_comes_from_the_shared_smarttscope_config(self, tmp_path: Path) -> None:
        shared = tmp_path / "smarttscope.toml"
        shared.write_text("[observer]\nlat = 47.5\nlon = 11.25\nheight_m = 300.0\n")
        config = load_onstep_indi_config(shared, tmp_path / "missing-own.toml")
        assert (config.observer_lat, config.observer_lon, config.observer_alt_m) == (
            47.5, 11.25, 300.0,
        )

    def test_indi_identity_and_meridian_limits_come_from_the_local_config(
        self, tmp_path: Path
    ) -> None:
        own = tmp_path / "config.toml"
        own.write_text(
            "[indi]\nhost = \"10.0.0.5\"\nport = 7625\ndevice = \"My OnStep\"\n"
            "home_motion_enabled = true\nfocuser_max_position = 20000\n"
            "[meridian]\nflip_request_deg = 90.0\ntracking_stop_deg = 105.0\n"
        )
        config = load_onstep_indi_config(tmp_path / "missing-shared.toml", own)
        assert (config.host, config.port, config.device) == ("10.0.0.5", 7625, "My OnStep")
        assert config.home_motion_enabled is True
        assert config.focuser_max_position == 20000
        assert (config.flip_request_deg, config.tracking_stop_deg) == (90.0, 105.0)

    def test_tracking_authority_policy_is_read_and_validated(self, tmp_path: Path) -> None:
        own = tmp_path / "config.toml"
        missing = tmp_path / "s.toml"

        def policy() -> str:
            return str(load_onstep_indi_config(missing, own).tracking_authority_policy)

        assert policy() == "strict"
        own.write_text("[indi]\ntracking_authority_policy = \"controller_managed\"\n")
        assert policy() == "controller_managed"
        own.write_text("[indi]\ntracking_authority_policy = \"anything-else\"\n")
        assert policy() == "strict"

    def test_malformed_values_fall_back_to_defaults(self, tmp_path: Path) -> None:
        own = tmp_path / "config.toml"
        own.write_text('[indi]\nport = "fast"\nhome_motion_enabled = "yes"\n')
        config = load_onstep_indi_config(tmp_path / "missing-shared.toml", own)
        assert config.port == 7624
        assert config.home_motion_enabled is False
