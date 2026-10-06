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

import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from astrotool_core.mount.park_port import MountParkStatus
from astrotool_core.mount.port import AxisDirection, MountAxis, MountStatus
from astrotool_core.onstep import (
    OnStepConnection,
    OnStepFocuserAdapter,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
    load_onstep_indi_config,
)
from astrotool_core.testing import (
    ObservableRLock,
    OnStepScenario,
    install_observable_operation_lock,
    make_simulated_onstep_connection,
)
from astrotool_core.testing.fake_onstep_indi_client import (
    FakeOnStepIndiClient,
    fake_indi_runtime_config,
    make_fake_onstep_indi_connection,
)
from astrotool_core.timing import FakeClock

_JOIN_S = 5.0  # real-time safety bound for test threads, never a policy wait


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

    def test_unpark_of_an_already_unparked_mount_is_refused(self) -> None:
        """Verified against the real published OnStepAdapter 0.4.1 wheel
        (not the pre-release dev build): `unpark()` still requires the
        mount to already be parked -- calling it on an already-unparked
        mount is refused, not silently accepted. `MountParkPanel` never
        makes this call itself (the Unpark button is only enabled while
        `status().parked`), so this only guards a direct/programmatic
        caller."""
        conn, made = _connection()
        park = OnStepMountParkAdapter(conn)
        park.connect()
        made[0].parked = False
        with pytest.raises(RuntimeError, match="did not reach unparked"):
            park.unpark()

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

    def test_rejected_moves_specific_reason_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """FocuserMoveResult is deliberately hardware-neutral (see its own
        docstring) -- a rejected move's real reason must still reach
        application.log (and hence a diagnostic bundle), since that's the
        only place it can surface. Real field report: a move_rejected
        autofocus run left `failure_reason: null` in its diagnostic bundle
        with zero corroborating log lines, making it undebuggable."""
        conn, made = _connection()
        f = OnStepFocuserAdapter(conn)
        f.connect()
        made[0].focuser.reject_moves = True
        with caplog.at_level("WARNING"):
            f.move_absolute(500)
        assert "rejected by fake" in caplog.text

    def test_a_bounds_rejected_move_logs_the_valueerror_reason(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        conn, made = _connection()
        f = OnStepFocuserAdapter(conn)
        f.connect()
        made[0].focuser.driver_maximum = 100
        with caplog.at_level("WARNING"):
            result = f.move_absolute(10_000)
        assert not result.accepted
        assert "exceeds confirmed travel limits" in caplog.text

    def test_controller_without_a_focuser_reports_unavailable(self) -> None:
        conn, made = _connection()
        f = OnStepFocuserAdapter(conn)
        f.connect()
        made[0].focuser.connected = False
        assert not f.is_available
        assert not f.status().available
        assert not f.move_absolute(10).accepted

    def test_blockers_passes_through_the_live_snapshot_not_just_a_bool(self) -> None:
        """Field bundle 7b21bdf1: a rejected move with no prior log line
        needs to show whatever precondition (e.g. focuser_position_stale)
        was already blocking it *before* the move was even attempted --
        status().available collapses this to one bool; blockers() must
        not."""
        conn, made = _connection()
        f = OnStepFocuserAdapter(conn)
        f.connect()
        assert f.blockers() == ()  # nothing blocking by default
        made[0].focuser.connected = False
        assert f.blockers() == ("indi_device_disconnected",)

    def test_blockers_is_empty_when_not_connected(self) -> None:
        f = OnStepFocuserAdapter(_connection()[0])
        assert f.blockers() == ()

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


class TestOperationSerialization:
    """Real field report (diagnostic 7b21bdf1): a focuser move kept getting
    rejected in the live app but succeeded every time in an isolated,
    single-threaded reproduction against the same live controller. Root
    cause: AutofocusRunner moves the focuser on a real background thread
    while FocuserPanel/MountTestMovePanel/MountParkPanel each poll
    status() on a QTimer (the GUI thread) -- all sharing one
    OnStepConnection. Nothing serialized a whole compound operation (e.g.
    move_absolute()'s read-check-send-wait sequence) against a concurrent,
    unrelated status() call arriving mid-sequence from another thread.
    These tests prove OnStepConnection.operation_lock actually closes that
    gap -- across a single adapter's own methods, and across two
    different adapter types sharing the same connection."""

    @staticmethod
    def _guarded(
        real: object, holder: list[object], overlap_detected: threading.Event
    ) -> Callable[..., object]:
        """Wraps a fake's method so entering it while a DIFFERENT thread's
        guarded call already holds the critical section is detectable --
        both sides of a potential race must check-and-set the same shared
        `holder` from *inside* the call under test, not from an external,
        unsynchronized spin loop (which would just race against the lock
        itself and prove nothing). Tracks the holding thread's identity,
        not a bare bool: the real adapter methods legitimately call each
        other on the *same* thread while holding operation_lock once
        (e.g. move_absolute() calling get_status() internally) -- that is
        correct reentrant behavior, not a race, and must not be flagged."""

        def wrapped(*args: object, **kwargs: object) -> object:
            me = threading.get_ident()
            current = holder[0]
            if current is not None and current != me:
                overlap_detected.set()
            holder[0] = me
            time.sleep(0.02)
            try:
                return real(*args, **kwargs)  # type: ignore[operator]
            finally:
                if holder[0] == me:
                    holder[0] = None

        return wrapped

    def test_a_slow_move_and_concurrent_status_polls_never_overlap(self) -> None:
        conn, made = _connection()
        focuser = OnStepFocuserAdapter(conn)
        focuser.connect()

        overlap_detected = threading.Event()
        holder: list[object] = [None]
        made[0].focuser.move_absolute = self._guarded(  # type: ignore[method-assign]
            made[0].focuser.move_absolute, holder, overlap_detected
        )
        made[0].focuser.get_status = self._guarded(  # type: ignore[method-assign]
            made[0].focuser.get_status, holder, overlap_detected
        )

        mover = threading.Thread(target=lambda: focuser.move_absolute(600))
        pollers = [threading.Thread(target=lambda: [focuser.status() for _ in range(10)])
                   for _ in range(3)]
        mover.start()
        for p in pollers:
            p.start()
        mover.join()
        for p in pollers:
            p.join()

        assert not overlap_detected.is_set()

    def test_operations_across_different_adapter_types_never_overlap(self) -> None:
        """The same hazard, but between TWO different adapters
        (focuser + mount park) sharing one OnStepConnection -- e.g.
        MountTestMovePanel's own GUI-thread poll of the mount while
        autofocus moves the focuser on a worker thread."""
        conn, made = _connection()
        focuser = OnStepFocuserAdapter(conn)
        park = OnStepMountParkAdapter(conn)
        focuser.connect()
        park.connect()

        overlap_detected = threading.Event()
        holder: list[object] = [None]
        made[0].focuser.get_status = self._guarded(  # type: ignore[method-assign]
            made[0].focuser.get_status, holder, overlap_detected
        )
        made[0].mount.get_status = self._guarded(  # type: ignore[method-assign]
            made[0].mount.get_status, holder, overlap_detected
        )

        def hammer_focuser() -> None:
            for _ in range(10):
                focuser.status()

        def hammer_park() -> None:
            for _ in range(10):
                park.status()

        t1 = threading.Thread(target=hammer_focuser)
        t2 = threading.Thread(target=hammer_park)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not overlap_detected.is_set()


class TestStopDuringAngularGoto:
    """S6.0c (#49; AGENTS.md "Manual movement versus measurement": bounded, cancellable, Qt
    event loop responsive). OnStepAdapter 0.4.1's `move_*_axis_deg` blocks for the whole GOTO
    and has no cancel hook; `move_angular` holds `operation_lock` for all of it (9cea2e9). On the
    #51 simulator with a manual FakeClock the GOTO stays in flight for as long as the test
    wants, and `ObservableRLock` tells deterministically whether a second thread is queued
    behind it ("blocked") or got through ("done") -- no timing, no real sleeps."""

    @staticmethod
    def _goto_in_flight(
        *, stop_confirm_latency_s: float = 0.0, read_first: bool = False
    ) -> tuple[
        FakeClock, ObservableRLock, OnStepMountPulseAdapter, Any, threading.Thread, list[Any]
    ]:
        clock = FakeClock(auto_advance=False)
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(
                parked=False,
                axis_rate_deg_per_s=0.1,
                stop_confirm_latency_s=stop_confirm_latency_s,
            ),
            clock=clock,
        )
        lock = install_observable_operation_lock(connection)
        mount = OnStepMountPulseAdapter(connection)
        mount.connect()
        if read_first:
            assert mount.status().connected
        results: list[Any] = []
        mover = threading.Thread(
            target=lambda: results.append(
                mount.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 300.0)
            ),
            daemon=True,
        )
        mover.start()
        assert clock.wait_for_sleepers(1), "the GOTO never started"
        return clock, lock, mount, made[0], mover, results

    @staticmethod
    def _finish(clock: FakeClock, *threads: threading.Thread) -> None:
        """Run fake time on until every worker is done (the GOTO's own 30 s deadline, then the
        stop confirmation waits). Real time only bounds it."""
        deadline = time.monotonic() + 5.0
        while any(t.is_alive() for t in threads):
            assert time.monotonic() < deadline, "workers never finished"
            clock.advance(30.0)
            threading.Event().wait(0.002)

    @staticmethod
    def _until(condition: Callable[[], bool], timeout_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while not condition():
            if time.monotonic() >= deadline:
                return False
            threading.Event().wait(0.001)
        return True

    def test_abort_does_not_wait_behind_the_running_goto(self) -> None:
        """Stop is called on the GUI thread: it must neither queue behind the GOTO's lock nor
        wait out OnStepAdapter's stop confirmation (2 s of fake time here, up to 5 s real)."""
        clock, lock, mount, _client, mover, _ = self._goto_in_flight(stop_confirm_latency_s=2.0)
        aborter = threading.Thread(target=mount.abort, daemon=True)
        aborter.start()
        try:
            assert lock.wait_until_blocked_or_done(aborter) == "done"
        finally:
            self._finish(clock, mover, aborter)

    def test_abort_reaches_the_mount_while_the_goto_is_still_running(self) -> None:
        clock, _lock, mount, client, mover, _ = self._goto_in_flight()
        aborter = threading.Thread(target=mount.abort, daemon=True)
        aborter.start()
        try:
            assert self._until(lambda: client.axis_moves_aborted == 1), (
                "OnStep's stop never reached the mount while the GOTO was running"
            )
        finally:
            self._finish(clock, mover, aborter)

    def test_a_stopped_goto_is_reported_as_stopped_not_as_a_move(self) -> None:
        clock, _lock, mount, client, mover, results = self._goto_in_flight()
        aborter = threading.Thread(target=mount.abort, daemon=True)
        aborter.start()
        self._until(lambda: client.axis_moves_aborted == 1)
        self._finish(clock, mover, aborter)

        (result,) = results
        assert not result.accepted
        assert "stopped by the user" in result.message

    def test_a_stop_while_the_move_is_queued_behind_another_operation_cancels_it_unsent(
        self,
    ) -> None:
        """`_cancel` was never checked: a Stop that lands while `move_angular` still waits for
        the shared connection (e.g. behind a park-panel action) must cancel it unsent."""
        clock = FakeClock(auto_advance=False)
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False), clock=clock
        )
        lock = install_observable_operation_lock(connection)
        mount = OnStepMountPulseAdapter(connection)
        mount.connect()
        results: list[Any] = []
        mover = threading.Thread(
            target=lambda: results.append(
                mount.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 300.0)
            ),
            daemon=True,
        )
        with lock:  # another operation holds the connection
            mover.start()
            assert lock.wait_until_blocked_or_done(mover) == "blocked"
            mount.abort()
        self._finish(clock, mover)

        assert made[0].axis_move_calls == []
        (result,) = results
        assert not result.accepted and "stopped by the user" in result.message

    def test_a_status_poll_during_the_goto_does_not_wait_and_reports_slewing(self) -> None:
        clock, lock, mount, client, mover, _ = self._goto_in_flight(read_first=True)
        statuses: list[Any] = []
        poller = threading.Thread(target=lambda: statuses.append(mount.status()), daemon=True)
        poller.start()
        try:
            assert lock.wait_until_blocked_or_done(poller) == "done"
            (status,) = statuses
            assert status.connected and status.slewing
            # 9cea2e9 still holds: the poll never read the controller mid-GOTO
            assert client.monitor.interleavings == []
        finally:
            self._finish(clock, mover, poller)

    def test_a_stop_is_remembered_until_the_next_command_is_armed(self) -> None:
        """A Stop between two steps of a sequence must also refuse the next step; the next
        operator action (`clear_abort`, called by whoever starts it) is not refused."""
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False), clock=clock
        )
        mount = OnStepMountPulseAdapter(connection)
        mount.connect()
        mount.abort()
        assert self._until(lambda: made[0].emergency_stop_calls == 1)
        refused = mount.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 300.0)
        assert not refused.accepted and "stopped by the user" in refused.message
        assert made[0].axis_move_calls == []

        mount.clear_abort()
        assert mount.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 300.0).accepted


class TestReadsDuringAGotoDoNotQueue:
    """S6.0c / D1: the reads the panels poll on the GUI thread -- park `status()`,
    `home_confirmed`, focuser `status()`/`is_moving()` -- never queue behind a mount GOTO
    holding `operation_lock`; they serve the last reading (9cea2e9 still holds: nothing reads
    the controller mid-operation). Before any reading exists they say "not known yet":
    unavailable / not confirmed. Compound operations still queue."""

    @staticmethod
    def _rig(
        *, read_first: bool
    ) -> tuple[
        FakeClock,
        ObservableRLock,
        OnStepMountParkAdapter,
        OnStepFocuserAdapter,
        Any,
        threading.Thread,
    ]:
        clock = FakeClock(auto_advance=False)
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, axis_rate_deg_per_s=0.1), clock=clock
        )
        lock = install_observable_operation_lock(connection)
        park, focuser = OnStepMountParkAdapter(connection), OnStepFocuserAdapter(connection)
        mount = OnStepMountPulseAdapter(connection)
        for adapter in (park, focuser, mount):
            adapter.connect()
        if read_first:
            assert park.status().available and park.home_confirmed
            assert focuser.status().available
        mover = threading.Thread(
            target=mount.move_angular,
            args=(MountAxis.AXIS1, AxisDirection.POSITIVE, 300.0),
            daemon=True,
        )
        mover.start()
        assert clock.wait_for_sleepers(1), "the GOTO never started"
        return clock, lock, park, focuser, made[0], mover

    @staticmethod
    def _read(lock: ObservableRLock, read: Callable[[], object]) -> tuple[str, list[object]]:
        got: list[object] = []
        reader = threading.Thread(target=lambda: got.append(read()), daemon=True)
        reader.start()
        return lock.wait_until_blocked_or_done(reader), got

    @staticmethod
    def _finish(clock: FakeClock, mover: threading.Thread) -> None:
        deadline = time.monotonic() + 5.0
        while mover.is_alive():
            assert time.monotonic() < deadline
            clock.advance(30.0)
            threading.Event().wait(0.002)

    def test_park_status_serves_the_last_reading(self) -> None:
        clock, lock, park, _focuser, client, mover = self._rig(read_first=True)
        try:
            outcome, got = self._read(lock, park.status)
            assert outcome == "done"
            assert got[0].available and not got[0].parked  # type: ignore[attr-defined]
            assert not got[0].fresh  # type: ignore[attr-defined]  # held over, never a decision
            assert client.monitor.interleavings == []
        finally:
            self._finish(clock, mover)

    def test_home_confirmed_serves_the_last_reading(self) -> None:
        clock, lock, park, _focuser, _client, mover = self._rig(read_first=True)
        try:
            outcome, got = self._read(lock, lambda: park.home_confirmed)
            assert outcome == "done" and got == [True]
        finally:
            self._finish(clock, mover)

    def test_focuser_status_and_is_moving_serve_the_last_reading(self) -> None:
        clock, lock, _park, focuser, client, mover = self._rig(read_first=True)
        try:
            outcome, got = self._read(lock, focuser.status)
            assert outcome == "done"
            assert got[0].available and got[0].position == 5000  # type: ignore[attr-defined]
            outcome, got = self._read(lock, focuser.is_moving)
            assert outcome == "done" and got == [False]
            assert client.monitor.interleavings == []
        finally:
            self._finish(clock, mover)

    def test_with_nothing_read_yet_a_busy_read_says_not_known(self) -> None:
        clock, lock, park, focuser, _client, mover = self._rig(read_first=False)
        try:
            assert self._read(lock, park.status) == (
                "done",
                [MountParkStatus(available=False, parked=False, tracking=False, fresh=False)],
            )
            assert self._read(lock, lambda: park.home_confirmed) == ("done", [False])
            outcome, got = self._read(lock, focuser.status)
            assert outcome == "done" and not got[0].available  # type: ignore[attr-defined]
        finally:
            self._finish(clock, mover)
        # once the connection is free, real readings again
        assert park.status().available and park.home_confirmed and focuser.status().available

    def test_a_focuser_move_still_queues_behind_the_goto(self) -> None:
        """9cea2e9's compound-operation serialization is untouched by D1."""
        clock, lock, _park, focuser, client, mover = self._rig(read_first=True)
        try:
            outcome, _ = self._read(lock, lambda: focuser.move_absolute(5100))
            assert outcome == "blocked"
        finally:
            self._finish(clock, mover)
            deadline = time.monotonic() + 5.0
            while client.monitor.active or client.focuser.move_log == []:
                assert time.monotonic() < deadline
                clock.advance(1.0)
                threading.Event().wait(0.002)
        assert client.monitor.interleavings == []

    def test_a_focuser_status_during_its_own_move_says_moving(self) -> None:
        clock = FakeClock(auto_advance=False)
        connection, _made = make_simulated_onstep_connection(OnStepScenario(), clock=clock)
        lock = install_observable_operation_lock(connection)
        focuser = OnStepFocuserAdapter(connection)
        focuser.connect()
        assert not focuser.status().moving
        mover = threading.Thread(target=lambda: focuser.move_absolute(6000), daemon=True)
        mover.start()
        assert clock.wait_for_sleepers(1)
        try:
            outcome, got = self._read(lock, focuser.status)
            assert outcome == "done" and got[0].moving  # type: ignore[attr-defined]
        finally:
            self._finish(clock, mover)


class TestStopWorkerSafety:
    """S6.0c review C3/P1/P2/P3: the Stop worker must stop what Stop covers -- never the
    operator's NEXT command -- serve every Stop, and never die silently; cached readings never
    survive a disconnect. Simulator on a manual FakeClock; OnStep's stop confirmation takes
    2 s of fake time, so the worker is provably still busy while the test acts."""

    @staticmethod
    def _rig() -> tuple[FakeClock, OnStepMountPulseAdapter, Any]:
        clock = FakeClock(auto_advance=False)
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(parked=False, axis_rate_deg_per_s=0.01, stop_confirm_latency_s=2.0),
            clock=clock,
        )
        mount = OnStepMountPulseAdapter(connection)
        mount.connect()
        return clock, mount, made[0]

    @staticmethod
    def _move(mount: OnStepMountPulseAdapter, results: list[Any]) -> threading.Thread:
        mover = threading.Thread(
            target=lambda: results.append(
                mount.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 300.0)
            ),
            daemon=True,
        )
        mover.start()
        return mover

    @staticmethod
    def _run_until_done(clock: FakeClock, *threads: threading.Thread) -> None:
        deadline = time.monotonic() + 5.0
        while any(t.is_alive() for t in threads):
            assert time.monotonic() < deadline, "workers never finished"
            clock.advance(1.0)
            threading.Event().wait(0.002)

    def test_a_late_second_stop_never_hits_the_operators_next_goto(self) -> None:
        """C3: Stop with nothing moving; while its stop is still confirming, the operator's
        next command re-arms and starts a GOTO. The worker's follow-up stop is only for GOTOs
        the Stop covered -- the new one runs to its target."""
        clock, mount, client = self._rig()
        mount.abort()
        worker = mount._stop_worker
        assert worker is not None and clock.wait_for_sleepers(1)  # first stop confirming
        mount.clear_abort()  # runner.submit() of the next nudge
        results: list[Any] = []
        mover = self._move(mount, results)
        assert clock.wait_for_sleepers(2)  # the new GOTO is travelling (300" at 0.01 deg/s)

        clock.advance(2.0)  # the first stop is confirmed; the worker decides about a second
        assert TestStopDuringAngularGoto._until(
            lambda: not worker.is_alive() or client.emergency_stop_calls >= 2
        )
        self._run_until_done(clock, mover, worker)

        assert client.axis_moves_aborted == 0
        assert client.emergency_stop_calls == 1
        (result,) = results
        assert result.accepted, result.message

    def test_a_stop_while_the_worker_is_busy_still_stops_a_newer_goto(self) -> None:
        """P1: a second Stop while the worker still confirms the first one is not dropped --
        a GOTO started (after a re-arm) since the worker's last ABORT is stopped too."""
        clock, mount, client = self._rig()
        mount.abort()
        worker = mount._stop_worker
        assert worker is not None and clock.wait_for_sleepers(1)
        mount.clear_abort()
        results: list[Any] = []
        mover = self._move(mount, results)
        assert clock.wait_for_sleepers(2)

        mount.abort()  # the operator stops the new GOTO too, worker still busy
        self._run_until_done(clock, mover, worker)

        assert client.axis_moves_aborted == 1
        (result,) = results
        assert not result.accepted and "stopped by the user" in result.message

    def test_a_failing_stop_is_logged_and_the_stop_stays_latched(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """P3: whatever OnStepAdapter raises, the worker logs it, ends cleanly, and Stop still
        refuses the next step unsent."""
        _clock, mount, client = self._rig()

        def broken_stop(**_kwargs: float) -> object:
            raise KeyError("ABORT")  # not one of the "expected" exception types

        client.emergency_stop = broken_stop
        with caplog.at_level(logging.ERROR):
            mount.abort()
            worker = mount._stop_worker
            assert worker is not None
            worker.join(_JOIN_S)
        assert not worker.is_alive()
        assert "Stop: OnStep emergency stop failed" in caplog.text

        refused = mount.move_angular(MountAxis.AXIS1, AxisDirection.POSITIVE, 300.0)
        assert not refused.accepted and "stopped by the user" in refused.message
        assert client.axis_move_calls == []

    def test_disconnect_drops_every_held_over_reading(self) -> None:
        """P2: a reading from an earlier session is never served after a reconnect; before
        the first new reading a busy read claims nothing."""
        clock = FakeClock()
        connection, _made = make_simulated_onstep_connection(
            OnStepScenario(parked=False), clock=clock
        )
        lock = install_observable_operation_lock(connection)
        park, focuser = OnStepMountParkAdapter(connection), OnStepFocuserAdapter(connection)
        mount = OnStepMountPulseAdapter(connection)
        for adapter in (park, focuser, mount):
            adapter.connect()
        assert park.status().available and focuser.status().available
        assert mount.status().connected and park.home_confirmed
        for adapter in (park, focuser, mount):
            adapter.disconnect()
        for adapter in (park, focuser, mount):
            adapter.connect()

        with lock:  # busy, as during a GOTO, and nothing read in this session yet
            reads: list[object] = []
            reader = threading.Thread(
                target=lambda: reads.extend(
                    [park.status(), park.home_confirmed, focuser.status(), mount.status()]
                ),
                daemon=True,
            )
            reader.start()
            reader.join(_JOIN_S)
        park_status, home, focuser_status, mount_status = reads
        assert not park_status.available and not park_status.fresh  # type: ignore[attr-defined]
        assert home is False
        assert not focuser_status.available  # type: ignore[attr-defined]
        assert not mount_status.connected  # type: ignore[attr-defined]

    def test_concurrent_stops_never_start_two_workers(self) -> None:
        """Re-review P-e: the worker is started inside the stop bookkeeping's critical section."""
        clock, mount, _client = self._rig()
        barrier = threading.Barrier(8)

        def press_stop() -> None:
            barrier.wait(_JOIN_S)
            mount.abort()

        pressers = [threading.Thread(target=press_stop, daemon=True) for _ in range(8)]
        for presser in pressers:
            presser.start()
        for presser in pressers:
            presser.join(_JOIN_S)
        workers = [t for t in threading.enumerate() if t.name == "onstep-mount-stop"]
        try:
            assert len(workers) == 1, workers
        finally:
            self._run_until_done(clock, *workers)


class TestHeldOverMountStatusIsMarked:
    """S6.5 (proofs/49-stop-during-angular-goto.toml): a `MountStatus` served from the last
    reading while another operation holds the connection is marked `fresh=False` -- display
    only, no decision can mistake it for a new reading (like `MountParkStatus.fresh`)."""

    def _busy_reads(self, *, read_first: bool) -> MountStatus:
        clock = FakeClock()
        connection, _made = make_simulated_onstep_connection(
            OnStepScenario(parked=False), clock=clock
        )
        lock = install_observable_operation_lock(connection)
        mount = OnStepMountPulseAdapter(connection)
        mount.connect()
        if read_first:
            assert mount.status().fresh is True  # a real read
        reads: list[MountStatus] = []
        with lock:  # another operation (a GOTO) holds the connection
            reader = threading.Thread(target=lambda: reads.append(mount.status()), daemon=True)
            reader.start()
            reader.join(_JOIN_S)
        (status,) = reads
        return status

    def test_a_held_over_reading_is_not_fresh(self) -> None:
        status = self._busy_reads(read_first=True)
        assert status.connected and status.fresh is False

    def test_nothing_read_yet_is_not_fresh(self) -> None:
        status = self._busy_reads(read_first=False)
        assert not status.connected and status.fresh is False
