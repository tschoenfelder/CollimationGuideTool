"""FakeOnStepIndiClient — in-memory stand-in for `onstep_adapter.OnStepIndiClient`
(OnStepAdapter >= 0.4.0, INDI-backed transport).

Models only the behaviours the `astrotool_core.onstep` shims depend on: a
parked/tracking mount refusing axis motion, park/unpark/go_home gated by
`home_motion_enabled`, `enable_tracking` refusing from an unsafe/untrusted
state (matching OnStepAdapter#14's fix -- tracking-enable is real now, not
always-`NotImplementedError`), `goto` always refusing (0.4.0 has no INDI
equivalent), and a motion lock on axis moves. Not a simulation of the INDI
wire protocol — `OnStepConnection` and the shims never see one; they only
see `OnStepIndiClient`'s own Python surface, so that is what this fake
reproduces (same convention as `fake_onstep_client.py` did for 0.3.5).

S6.0d (#39) / S6.0e: OnStepAdapter 0.5.0's tracking-preserving guide pulse. Since S6.0e the
published 0.5.0 is pinned and this fake's DEFAULT facade is 0.5.0's (`guide_pulse_api=True`);
`guide_pulse_api=False` together with `simulate_onstep_adapter_041_package` is the published
0.4.1 surface (no `guide_pulse` on the facade, no guide exports), kept for the capability-absent
path. Modelled line by line on the PUBLISHED 0.5.0 wheel (release v0.5.0, sha256 2420ba1c...;
`tests/contracts/test_onstep_guide_pulse_contract.py` replays it against the installed
`IndiGuideController`): the facade `IndiMount.guide_pulse`/`guide` (indi_mount.py:32-42),
`OnStepIndiClient.guide_pulse` raising ConnectionError when not connected (indi_client.py:243-250)
and building the controller with the configured `tracking_authority_policy`
(indi_client.py:198-203), and `IndiGuideController` (indi_guiding.py): bounds 20..5000 ms and
500 ms chunks (:17-19), direction and integer-duration validation raising ValueError (:110-115,
:161-169), the chunk split (:171-179), the non-blocking lock answering "another guide pulse is
active" (:181-185), a preflight before EVERY chunk (:194-204) with its hard / tracking-state /
meridian / strict-authority refusals and its warnings (:117-152), after each issued chunk the
wait for a fresh OnStep status with the `:GU#` `G` flag cleared (:221-227, :282-303) -- whose
preflight refusal is raised (`_GuideSafetyError`, :53-61) and whose expiry is a TimeoutError --
one more preflight after the last chunk (:230-235), and -- on a ConnectionError/RuntimeError/
TimeoutError/ValueError once a chunk was issued, including those post-chunk refusals --
`emergency_stop()` (ABORT + TRACK_OFF) before the failed result is returned (:241-258). The
preflight's snapshot blockers are the ones 0.5.0's status reader derives (indi_status.py:74-127)
from this fake's flags, plus `guide_blockers` to inject any other; the meridian phase follows
`classify_meridian` (indi_meridian.py:28-75), with the geometric phase itself configured
(`meridian_phase`) instead of computed from hour angle and site. The axis moves at
`guide_rate_x` x sidereal per issued chunk (0.5.0 exposes no guide rate; the real rate is the
controller's).
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Protocol

import onstep_adapter
from onstep_adapter import (
    IndiFocuserMoveResult,
    IndiFocuserSnapshot,
    IndiMeridianState,
    IndiMountSnapshot,
    IndiPositionResult,
    IndiRuntimeConfig,
    IndiStartupStatus,
    IndiStopResult,
    IndiUnparkResult,
)

from astrotool_core.onstep.connection import OnStepConnection

#: Public names, including the OnStepAdapter result/snapshot types re-exported
#: for the #51 simulators (`sim_onstep`): only this module may import
#: onstep_adapter (import-linter "OnStepAdapter is the only route" ignore list).
__all__ = [
    "FakeIndiAxisMoveResult",
    "FakeIndiFocuser",
    "FakeIndiGuideMount",
    "FakeIndiGuidePulseResult",
    "FakeIndiMount",
    "FakeIndiTrackingResult",
    "FakeOnStepIndiClient",
    "IndiFocuserMoveResult",
    "IndiFocuserSnapshot",
    "IndiMountSnapshot",
    "IndiPositionResult",
    "IndiRuntimeConfig",
    "IndiStartupStatus",
    "IndiStopResult",
    "IndiUnparkResult",
    "GUIDE_IDLE_TIMEOUT_MESSAGE",
    "ONSTEP_ADAPTER_050_GUIDE_EXPORTS",
    "ONSTEP_ADAPTER_050_ONLY_EXPORTS",
    "fake_indi_runtime_config",
    "make_fake_onstep_indi_connection",
    "simulate_onstep_adapter_041_package",
]


@dataclass
class FakeIndiAxisMoveResult:
    axis: str
    requested_deg: float
    measured_deg: float
    stop_confirmed: bool = True


#: OnStepAdapter 0.5.0's guide bounds (indi_guiding.py:17-19), exported by its package
#: (0.5.0 `__init__.py`:7-12) -- the published 0.4.1 has none of them. Kept as this fake's own
#: copy (it must not change with the installed package); a contract test pins it to the
#: installed 0.5.0's values.
ONSTEP_ADAPTER_050_GUIDE_EXPORTS: dict[str, int] = {
    "MIN_GUIDE_PULSE_MS": 20,
    "MAX_GUIDE_PULSE_MS": 5000,
    "GUIDE_CHUNK_MS": 500,
}
_GUIDE_MIN_MS = ONSTEP_ADAPTER_050_GUIDE_EXPORTS["MIN_GUIDE_PULSE_MS"]
_GUIDE_MAX_MS = ONSTEP_ADAPTER_050_GUIDE_EXPORTS["MAX_GUIDE_PULSE_MS"]
_GUIDE_CHUNK_MS = ONSTEP_ADAPTER_050_GUIDE_EXPORTS["GUIDE_CHUNK_MS"]
#: Every name the published 0.5.0 `__init__.py` adds over 0.4.1 (`__init__.py`:7-12, `__all__`).
ONSTEP_ADAPTER_050_ONLY_EXPORTS: tuple[str, ...] = (
    *ONSTEP_ADAPTER_050_GUIDE_EXPORTS, "IndiGuidePulseResult",
)
#: 0.5.0's error when no fresh `G`-cleared status follows a chunk (indi_guiding.py:299-302).
GUIDE_IDLE_TIMEOUT_MESSAGE = (
    "OnStep still reports guide pulse active or did not publish a fresh post-guide status"
)
#: Sidereal rate (arcsec/s) for turning a guide chunk at `guide_rate_x` into an axis offset.
_SIDEREAL_ARCSEC_PER_S = 15.041

_GUIDE_DIRECTION_ALIASES = {  # indi_guiding.py:28-33
    "n": "north", "north": "north",
    "s": "south", "south": "south",
    "e": "east", "east": "east",
    "w": "west", "west": "west",
}
_GUIDE_HARD_BLOCKERS = frozenset({  # indi_guiding.py:35-39
    "indi_device_disconnected", "onstep_status_not_fresh",
    "onstep_status_alert", "onstep_status_unavailable",
    "onstep_limit_or_park_fault", "onstep_reported_error",
})
_GUIDE_AUTHORITY_BLOCKERS = frozenset({  # indi_guiding.py:41-45
    "coordinates_not_fresh", "pier_side_conflict", "pier_side_unknown",
    "coordinates_invalid", "time_site_authority_unestablished",
    "hour_angle_unavailable", "home_authority_unestablished",
})
_GUIDE_STRICT_ALLOWED_PHASES = frozenset({  # indi_guiding.py:47-50
    "pre_meridian_allowed", "post_meridian_allowed", "post_flip", "flip_required",
})


@dataclass(frozen=True)
class FakeIndiGuidePulseResult:
    """Field for field 0.5.0's `IndiGuidePulseResult` (indi_guiding.py:64-76)."""

    direction: str
    requested_duration_ms: int
    chunks_requested: int
    chunks_completed: int
    command_accepted: bool
    pulse_completed: bool
    tracking_preserved: bool
    final_raw_status: str | None
    meridian_phase: str
    warnings: tuple[str, ...] = ()
    error: str | None = None


class _FakeGuideSafetyError(RuntimeError):
    """0.5.0's `_GuideSafetyError` (indi_guiding.py:53-61): a refusal while waiting for the
    post-chunk status, carrying that preflight's snapshot, meridian state and warnings."""

    def __init__(
        self, message: str, snapshot: IndiMountSnapshot,
        meridian: IndiMeridianState, warnings: tuple[str, ...],
    ) -> None:
        super().__init__(message)
        self.snapshot = snapshot
        self.meridian = meridian
        self.warnings = warnings


class _MonkeyPatch(Protocol):
    def setattr(self, target: object, name: str, value: object, raising: bool = ...) -> None: ...

    def delattr(self, target: object, name: str, raising: bool = ...) -> None: ...


def simulate_onstep_adapter_041_package(monkeypatch: _MonkeyPatch) -> None:
    """Make the installed (0.5.0) `onstep_adapter` package look like the published 0.4.1 wheel
    to this app: none of 0.5.0's added exports, no `IndiMount.guide_pulse`/`guide`, version
    "0.4.1". Pair it with a 0.4.1 client facade (`guide_pulse_api=False`, or
    `OnStepScenario(guide_pulses=None)`), as a real 0.4.1 install has neither.
    `monkeypatch`: pytest's fixture (restores everything after the test)."""
    for name in ONSTEP_ADAPTER_050_ONLY_EXPORTS:
        monkeypatch.delattr(onstep_adapter, name, raising=False)
    for name in ("guide_pulse", "guide"):
        monkeypatch.delattr(onstep_adapter.IndiMount, name, raising=False)
    monkeypatch.setattr(onstep_adapter, "__version__", "0.4.1")


@dataclass
class FakeIndiTrackingResult:
    command_accepted: bool
    tracking_confirmed: bool
    error: str | None = None


class FakeIndiMount:
    """`client.mount` — mirrors `onstep_adapter.IndiMount`'s facade shape."""

    def __init__(self, client: FakeOnStepIndiClient) -> None:
        self._client = client

    def get_status(self) -> IndiMountSnapshot:
        return self._client.observe_mount()

    def unpark(self) -> IndiUnparkResult:
        return self._client.unpark()

    def go_home(self) -> IndiPositionResult:
        return self._client.go_home()

    def park(self) -> IndiPositionResult:
        return self._client.park()

    def stop(self) -> IndiStopResult:
        return self._client.emergency_stop()

    def meridian_status(self) -> IndiMeridianState | None:
        return None

    def move_ra_axis_deg(
        self, offset_deg: float, *, timeout_s: float = 30.0
    ) -> FakeIndiAxisMoveResult:
        return self._client.move_axis_deg("ra", offset_deg, timeout_s=timeout_s)

    def move_dec_axis_deg(
        self, offset_deg: float, *, timeout_s: float = 30.0
    ) -> FakeIndiAxisMoveResult:
        return self._client.move_axis_deg("dec", offset_deg, timeout_s=timeout_s)

    def goto(self, ra_hours: float, dec_deg: float) -> None:
        raise NotImplementedError(
            "INDI goto is unavailable until the 0.4 safety and HOME gates are validated"
        )

    def enable_tracking(self) -> FakeIndiTrackingResult:
        return self._client.enable_tracking()


class FakeIndiGuideMount(FakeIndiMount):
    """0.5.0's facade: `FakeIndiMount` plus `guide_pulse`/`guide` (indi_mount.py:32-42) and a
    real meridian classification (indi_mount.py:44-45)."""

    def guide_pulse(
        self, direction: str, duration_ms: int, *, command_timeout: float = 3.0
    ) -> FakeIndiGuidePulseResult:
        return self._client.guide_pulse(direction, duration_ms, command_timeout=command_timeout)

    def guide(self, direction: str, duration_ms: int) -> bool:
        return self.guide_pulse(direction, duration_ms).pulse_completed

    def meridian_status(self) -> IndiMeridianState | None:
        return self._client.meridian_state()


class FakeIndiFocuser:
    def __init__(self) -> None:
        self.position: int | None = 0
        self.driver_maximum: int | None = 10000
        self.configured_maximum: int | None = 10000
        self.moving = False
        self.connected = True
        self.reject_moves = False
        self.stop_calls = 0

    def get_status(self) -> IndiFocuserSnapshot:
        blockers: tuple[str, ...] = () if self.connected else ("indi_device_disconnected",)
        move_ready = not blockers and not self.moving and self.position is not None
        return IndiFocuserSnapshot(
            self.position, self.driver_maximum, self.configured_maximum,
            self.moving, bool(move_ready), blockers,
        )

    def move_absolute(self, target: int, *, timeout: float = 30.0) -> IndiFocuserMoveResult:
        before = self.get_status()
        if not before.move_ready:
            return IndiFocuserMoveResult(
                target, False, before.position, None,
                f"Focuser move refused: {', '.join(before.blockers) or 'not ready'}",
            )
        maximum = self.driver_maximum or 0
        if target < 0 or target > maximum:
            raise ValueError("Focuser target exceeds confirmed travel limits")
        if self.reject_moves:
            return IndiFocuserMoveResult(target, False, self.position, None, "rejected by fake")
        self.position = target
        return IndiFocuserMoveResult(target, True, target, None, None)

    def stop(self, *, timeout: float = 5.0) -> bool:
        self.stop_calls += 1
        return True


@dataclass
class FakeOnStepIndiClient:
    """Constructor-compatible with `OnStepIndiClient(config=...)`."""

    config: IndiRuntimeConfig
    mount: FakeIndiMount = field(init=False)
    focuser: FakeIndiFocuser = field(default_factory=FakeIndiFocuser)

    connect_ok: bool = True
    closed: bool = True
    connect_calls: int = 0

    tracking: bool = False
    slewing: bool = False
    parked: bool = True
    at_home: bool = False
    at_limit: bool = False
    pier_side: str = "east"
    ha_deg: float = -30.0
    dec_deg: float = 20.0
    home_authority_established: bool = True
    time_site_authority: bool = True

    #: Controls `park`/`unpark`/`go_home` — mirrors `IndiRuntimeConfig.home_motion_enabled`.
    unpark_rejected: bool = False
    park_rejected: bool = False
    go_home_rejected: bool = False
    #: Controls `emergency_stop`/`stop_tracking`.
    stop_confirms: bool = True
    #: Controls `enable_tracking`/`start_tracking` once preflight passes.
    tracking_rejected: bool = False

    axis_move_calls: list[tuple[str, float]] = field(default_factory=list)
    #: One finite axis move at a time, like the real `IndiAxisMover`.
    _axis_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    #: OnStepAdapter 0.5.0's `guide_pulse` on the mount facade (S6.0e: the default, as pinned);
    #: False = the published 0.4.1 surface without it. See the module docstring.
    guide_pulse_api: bool = True
    #: Guide rate as a multiple of sidereal, per normalized direction (missing = `guide_rate_x`).
    guide_rate_x: float = 0.5
    guide_direction_rate_x: dict[str, float] = field(default_factory=dict)
    #: The geometric meridian phase `classify_meridian` would compute from the hour angle.
    meridian_phase: str = "pre_meridian_allowed"
    #: Further status-reader blockers (e.g. "pier_side_unknown", "onstep_reported_error").
    guide_blockers: tuple[str, ...] = ()
    #: Raised by the next issued chunks' completion waits (None = that chunk completes).
    guide_chunk_errors: list[BaseException | None] = field(default_factory=list)
    #: Per issued chunk that completed: True = OnStep never publishes a fresh status with the
    #: `G` flag cleared, so 0.5.0's post-chunk wait expires (indi_guiding.py:282-303).
    guide_idle_timeouts: list[bool] = field(default_factory=list)
    #: Every `guide_pulse` call (normalized direction, ms) and every chunk actually issued.
    guide_pulse_calls: list[tuple[str, int]] = field(default_factory=list)
    guide_chunks_issued: list[tuple[str, int]] = field(default_factory=list)
    _guide_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.mount = FakeIndiGuideMount(self) if self.guide_pulse_api else FakeIndiMount(self)

    # ---- connection lifecycle ---------------------------------------
    def connect(self, *, timeout: float = 5.0) -> IndiStartupStatus:
        self.connect_calls += 1
        if not self.connect_ok:
            raise ConnectionError("fake OnStep INDI device is not connected")
        self.closed = False
        return IndiStartupStatus(
            device_connected=True,
            safe_meridian_flip_enabled=True,
            meridian_policy=None,
            controller_time_verified=False,
            astronomical_motion_ready=False,
            time_snapshot_age_seconds=0.0,
        )

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeOnStepIndiClient:
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---- status --------------------------------------------------------
    def observe_mount(self) -> IndiMountSnapshot:
        return IndiMountSnapshot(
            raw_status=None,
            motion_state="tracking" if self.tracking else "stopped",
            pier_side=self.pier_side,
            ra_hours=0.0,
            dec_deg=self.dec_deg,
            ha_deg=self.ha_deg,
            meridian_distance_deg=0.0,
            tracking=self.tracking,
            slewing=self.slewing,
            parked=self.parked,
            at_home=self.at_home,
            at_limit=self.at_limit,
            status_age_ms=0.0,
            coordinates_age_ms=0.0,
            status_live=True,
            coordinates_live=True,
            time_site_authority=self.time_site_authority,
            home_authority=self.home_authority_established,
            mechanical_safe=True,
            motion_refused=False,
            blockers=(),
        )

    # ---- park/home/emergency-stop ---------------------------------------
    def _require_home_motion(self) -> None:
        if not self.config.home_motion_enabled:
            raise RuntimeError(
                "INDI HOME/PARK motion is disabled pending the supervised H-status check"
            )

    def unpark(self, *, timeout: float = 20.0) -> IndiUnparkResult:
        """Real 0.4.1 release (verified against the published wheel, not
        the pre-release dev build this fake originally mirrored): `unpark()`
        still REQUIRES the mount to already be parked going in -- calling it
        on an already-unparked mount is refused ("Fresh parked, stationary
        and fault-free status is required"), not silently accepted. What
        OnStepAdapter#17 actually fixed was the wire-level confirmation
        (continuously-refreshed status, not a per-call accept-current
        hack) -- irrelevant to this fake, which has no wire timing to model.
        Does NOT touch `at_home`."""
        self._require_home_motion()
        if self.unpark_rejected:
            return IndiUnparkResult(False, "unpark", None, "fake rejected unpark", None)
        if not self.parked or self.slewing:
            return IndiUnparkResult(
                False, "preflight", None,
                "Fresh parked, stationary and fault-free status is required", None,
            )
        self.parked = False
        self.tracking = False
        return IndiUnparkResult(True, "unparked", None, None, None)

    def go_home(self, *, timeout: float = 120.0) -> IndiPositionResult:
        self._require_home_motion()
        if self.go_home_rejected:
            return IndiPositionResult("home", False, None, "fake rejected go_home", None)
        self.at_home = True
        self.tracking = False
        return IndiPositionResult("home", True, None, None, None)

    def park(self, *, timeout: float = 120.0) -> IndiPositionResult:
        """0.4.1 behavior: parks directly from any stationary, non-tracking
        state (no at-HOME requirement -- OnStepAdapter#16); already parked
        is an immediate success."""
        self._require_home_motion()
        if self.park_rejected:
            return IndiPositionResult("park", False, None, "fake rejected park", None)
        if self.slewing or self.tracking:
            return IndiPositionResult(
                "park", False, None,
                "Fresh stationary, non-tracking and fault-free status is required", None,
            )
        self.parked = True
        self.at_home = False
        return IndiPositionResult("park", True, None, None, None)

    def emergency_stop(self, *, timeout: float = 5.0) -> IndiStopResult:
        if self.stop_confirms:
            self.tracking = False
            self.slewing = False
        return IndiStopResult(
            abort_accepted=True,
            tracking_off_accepted=True,
            stopped_confirmed=self.stop_confirms,
            consecutive_stopped_polls=2 if self.stop_confirms else 0,
            last_raw_status=None,
            errors=() if self.stop_confirms else ("fake stop did not confirm",),
        )

    # ---- tracking-enable (real since OnStepAdapter#14's fix) -------------
    def enable_tracking(self, *, timeout: float = 8.0) -> FakeIndiTrackingResult:
        """Mirrors `enable_tracking_via_indi`: "strict" (the default policy)
        also demands home/time-site authority and not-at-home;
        "controller_managed" only refuses parked/slewing/at_limit (the real
        check also gates on meridian phase, not modeled here)."""
        hard = self.parked or self.slewing or self.at_limit
        strict = self.config.tracking_authority_policy == "strict" and (
            self.at_home or not self.home_authority_established
            or not self.time_site_authority
        )
        if hard or strict:
            return FakeIndiTrackingResult(False, False, "fake tracking preflight refused")
        if self.tracking:
            return FakeIndiTrackingResult(False, True, None)
        if self.tracking_rejected:
            return FakeIndiTrackingResult(True, False, "fake tracking not confirmed")
        self.tracking = True
        return FakeIndiTrackingResult(True, True, None)

    # ---- axis motion (>= this dev build) --------------------------------
    #: Home authority/site-time authority and the fixed astronomical
    #: safety corridor were REMOVED from this primitive in the build this
    #: fake now mirrors -- only tracking/slewing/parked/at_limit still
    #: block a move (plus a flat -80..80 deg DEC bound, not modeled here
    #: since nothing in this app's tests exercises it).
    def move_axis_deg(
        self, axis: str, offset_deg: float, *, timeout_s: float = 30.0, poll_s: float = 0.1
    ) -> FakeIndiAxisMoveResult:
        minimum_deg = 30.0 / 3600.0
        if not math.isfinite(offset_deg) or not minimum_deg <= abs(offset_deg) <= 10.0:
            raise ValueError("Axis move must be between 30 arcseconds and 10 degrees")
        if not self._axis_lock.acquire(blocking=False):
            raise RuntimeError("Another axis motion is active")
        try:
            if self.tracking or self.slewing or self.parked or self.at_limit:
                raise RuntimeError(
                    "Local axis motion requires fresh unparked, stationary, "
                    "non-tracking and fault-free state"
                )
            self.axis_move_calls.append((axis, offset_deg))
            if axis == "ra":
                self.ha_deg += offset_deg
            else:
                self.dec_deg += offset_deg
            self.tracking = False
            return FakeIndiAxisMoveResult(axis, offset_deg, offset_deg)
        finally:
            self._axis_lock.release()

    # ---- 0.5.0 guide pulses (opt-in, see the module docstring) -----------------
    def _guide_snapshot_blockers(self, snapshot: IndiMountSnapshot) -> frozenset[str]:
        """The blockers 0.5.0's status reader adds for this fake's flags (indi_status.py:92-93,
        :112-114, :119-127) plus the injected `guide_blockers`."""
        blockers = set(snapshot.blockers) | set(self.guide_blockers)
        if snapshot.at_limit:
            blockers.add("onstep_limit_or_park_fault")
        if not snapshot.time_site_authority:
            blockers |= {"time_site_authority_unestablished", "hour_angle_unavailable"}
        if not snapshot.home_authority:
            blockers.add("home_authority_unestablished")
        if snapshot.parked or snapshot.at_home:
            blockers.add("mechanical_terminal_state")
        return frozenset(blockers)

    def _meridian_for(self, snapshot: IndiMountSnapshot) -> IndiMeridianState:
        """`classify_meridian` (indi_meridian.py:28-75), the geometric phase configured."""
        blockers = tuple(sorted(self._guide_snapshot_blockers(snapshot)))

        def state(phase: str, flip: bool, stop: bool, warn: bool) -> IndiMeridianState:
            return IndiMeridianState(
                phase, snapshot.ha_deg, snapshot.pier_side, flip, stop, None, None, None,
                warn, blockers,
            )

        if snapshot.parked or snapshot.at_home:
            return state("mechanical_terminal", False, False, False)
        if snapshot.at_limit and snapshot.status_live:
            return state("firmware_limit", False, snapshot.tracking or snapshot.slewing, True)
        if (
            not snapshot.status_live or not snapshot.coordinates_live
            or not snapshot.time_site_authority or snapshot.ha_deg is None
            or snapshot.pier_side is None or "pier_side_conflict" in blockers
            or "indi_device_disconnected" in blockers
        ):
            return state("unknown", False, False, False)
        phase = self.meridian_phase
        flip = phase in ("flip_required", "hard_stop")
        stop = phase == "hard_stop" and (snapshot.tracking or snapshot.slewing)
        return state(phase, flip, stop, flip or stop)

    def meridian_state(self) -> IndiMeridianState:
        return self._meridian_for(self.observe_mount())

    def _guide_preflight(
        self,
    ) -> tuple[IndiMountSnapshot, IndiMeridianState, tuple[str, ...], str | None]:
        """indi_guiding.py:117-152, in its order and with its texts."""
        snapshot = self.observe_mount()
        meridian = self._meridian_for(snapshot)
        blockers = self._guide_snapshot_blockers(snapshot)
        hard = _GUIDE_HARD_BLOCKERS & blockers
        authority = _GUIDE_AUTHORITY_BLOCKERS & blockers
        policy = self.config.tracking_authority_policy
        warnings = set(authority if policy == "controller_managed" else ())
        if meridian.flip_required:
            warnings.add("meridian_flip_required")
        reason = None
        if hard or not snapshot.status_live:
            reason = f"guide safety inputs unavailable: {sorted(hard)}"
        elif (
            not snapshot.tracking or snapshot.parked or snapshot.at_home
            or snapshot.slewing or snapshot.at_limit
        ):
            reason = (
                "guide pulse requires fresh unparked tracking state with no "
                "slew, HOME, fault or limit"
            )
        elif meridian.tracking_stop_required or meridian.phase in {"hard_stop", "firmware_limit"}:
            reason = f"guide pulse refused at meridian phase {meridian.phase}"
        elif policy == "strict" and (
            authority or not snapshot.coordinates_live
            or not snapshot.time_site_authority or not snapshot.home_authority
            or meridian.phase not in _GUIDE_STRICT_ALLOWED_PHASES
        ):
            reason = (
                f"guide astronomical authority unavailable: {sorted(authority)} "
                f"phase={meridian.phase}"
            )
        return snapshot, meridian, tuple(sorted(warnings)), reason

    def _guide_chunk(self, direction: str, chunk_ms: int) -> None:
        """One issued chunk until its TIMED_GUIDE property reports completion
        (indi_guiding.py:205-215, :262-280): the axis moves at the guide rate; a queued
        `guide_chunk_errors` entry is raised as that wait's failure (an INDI Alert ->
        RuntimeError, no completion -> TimeoutError). The simulator adds the clock time."""
        rate_x = self.guide_direction_rate_x.get(direction, self.guide_rate_x)
        offset_deg = rate_x * _SIDEREAL_ARCSEC_PER_S * chunk_ms / 1000.0 / 3600.0
        if direction == "west":  # increases hour angle, like a positive RA-axis move
            self.ha_deg += offset_deg
        elif direction == "east":
            self.ha_deg -= offset_deg
        elif direction == "north":
            self.dec_deg += offset_deg
        else:
            self.dec_deg -= offset_deg
        if self.guide_chunk_errors:
            error = self.guide_chunk_errors.pop(0)
            if error is not None:
                raise error

    def _guide_wait_idle(
        self, command_timeout: float
    ) -> tuple[IndiMountSnapshot, IndiMeridianState, tuple[str, ...]]:
        """0.5.0's post-chunk wait (indi_guiding.py:282-303): a preflight on the fresh status --
        a refusal raises `_FakeGuideSafetyError` (-> emergency stop, chunk not counted); with
        the `G` flag cleared the chunk is done. A queued `guide_idle_timeouts` True keeps `G`
        set: `_guide_idle_wait_elapse` lets the wait time pass, the preflight runs once more
        (still refusing first, as the real loop does) and the wait expires with TimeoutError."""
        stuck = bool(self.guide_idle_timeouts) and self.guide_idle_timeouts.pop(0)
        snapshot, meridian, warnings, refusal = self._guide_preflight()
        if refusal:
            raise _FakeGuideSafetyError(refusal, snapshot, meridian, warnings)
        if not stuck:
            return snapshot, meridian, warnings
        self._guide_idle_wait_elapse(command_timeout)
        snapshot, meridian, warnings, refusal = self._guide_preflight()
        if refusal:
            raise _FakeGuideSafetyError(refusal, snapshot, meridian, warnings)
        raise TimeoutError(GUIDE_IDLE_TIMEOUT_MESSAGE)

    def _guide_idle_wait_elapse(self, seconds: float) -> None:
        """The time a post-chunk wait spends before expiring; no clock here (the simulator
        adds it)."""

    def guide_pulse(
        self, direction: str, duration_ms: int, *, command_timeout: float = 3.0
    ) -> FakeIndiGuidePulseResult:
        """`OnStepIndiClient.guide_pulse` + `IndiGuideController.pulse` of 0.5.0."""
        if self.closed:  # indi_client.py:246-247
            raise ConnectionError("INDI client is not connected")
        normalized = _guide_arguments(direction, duration_ms, command_timeout)
        self.guide_pulse_calls.append((normalized, duration_ms))
        chunks = _guide_chunks(duration_ms)

        if not self._guide_lock.acquire(blocking=False):  # :181-185
            return FakeIndiGuidePulseResult(
                normalized, duration_ms, len(chunks), 0, False, False, False,
                None, "unknown", error="another guide pulse is active",
            )
        issued = False
        completed = 0
        warnings: set[str] = set()
        last_snapshot: IndiMountSnapshot | None = None
        last_meridian: IndiMeridianState | None = None
        try:
            for chunk in chunks:  # :193-228
                snapshot, meridian, current, refusal = self._guide_preflight()
                last_snapshot, last_meridian = snapshot, meridian
                warnings.update(current)
                if refusal:
                    return FakeIndiGuidePulseResult(
                        normalized, duration_ms, len(chunks), completed, issued, False,
                        snapshot.tracking, snapshot.raw_status, meridian.phase,
                        tuple(sorted(warnings)), refusal,
                    )
                self.guide_chunks_issued.append((normalized, chunk))
                issued = True
                self._guide_chunk(normalized, chunk)
                last_snapshot, last_meridian, current = self._guide_wait_idle(  # :221-227
                    command_timeout
                )
                warnings.update(current)
                completed += 1
            snapshot, meridian, current, refusal = self._guide_preflight()  # :230-235
            last_snapshot, last_meridian = snapshot, meridian
            warnings.update(current)
            if refusal:
                raise RuntimeError(refusal)
            return FakeIndiGuidePulseResult(
                normalized, duration_ms, len(chunks), completed, issued, True,
                snapshot.tracking, snapshot.raw_status, meridian.phase,
                tuple(sorted(warnings)), None,
            )
        except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:  # :241-258
            if isinstance(exc, _FakeGuideSafetyError):
                last_snapshot, last_meridian = exc.snapshot, exc.meridian
                warnings.update(exc.warnings)
            stop_error = None
            if issued:
                try:
                    self.emergency_stop()
                except (ConnectionError, RuntimeError, TimeoutError, ValueError) as stop_exc:
                    stop_error = f"; emergency stop failed: {stop_exc}"
            return FakeIndiGuidePulseResult(
                normalized, duration_ms, len(chunks), completed, issued, False,
                bool(last_snapshot and last_snapshot.tracking),
                last_snapshot.raw_status if last_snapshot else None,
                last_meridian.phase if last_meridian else "unknown",
                tuple(sorted(warnings)), f"{exc}{stop_error or ''}",
            )
        finally:
            self._guide_lock.release()


def _guide_arguments(direction: str, duration_ms: int, command_timeout: float) -> str:
    """0.5.0's argument checks, in its order and with its texts (indi_guiding.py:110-115,
    :161-169); returns the normalized direction."""
    try:
        normalized = _GUIDE_DIRECTION_ALIASES[direction.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError("Guide direction must be north, south, east or west") from exc
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int):
        raise ValueError("Guide duration must be an integer number of milliseconds")
    if not _GUIDE_MIN_MS <= duration_ms <= _GUIDE_MAX_MS:
        raise ValueError(f"Guide duration must be {_GUIDE_MIN_MS}..{_GUIDE_MAX_MS} ms")
    if not math.isfinite(command_timeout) or command_timeout <= 0:
        raise ValueError("Guide command timeout must be positive and finite")
    return normalized


def _guide_chunks(duration_ms: int) -> list[int]:
    """0.5.0's chunk split (indi_guiding.py:171-179): 500 ms chunks, never a remainder below
    the 20 ms minimum."""
    chunks: list[int] = []
    remaining = duration_ms
    while remaining > _GUIDE_CHUNK_MS:
        chunk = _GUIDE_CHUNK_MS
        if remaining - chunk < _GUIDE_MIN_MS:
            chunk = remaining - _GUIDE_MIN_MS
        chunks.append(chunk)
        remaining -= chunk
    chunks.append(remaining)
    return chunks


def fake_indi_runtime_config(**overrides: object) -> IndiRuntimeConfig:
    """A valid `IndiRuntimeConfig` for tests, with every field defaulted so
    a test only needs to override what it cares about.

    `home_motion_enabled=True` (unlike production's safe-by-default
    `load_onstep_indi_config`) -- this is a synthetic test double, not a
    real controller, so there is nothing for the "pending the supervised
    HOME test" gate to protect here; defaulting it off would just make
    every park/unpark/go_home contract test raise for a reason unrelated to
    what they're actually checking."""
    defaults: dict[str, object] = dict(
        host="127.0.0.1",
        port=7624,
        device="LX200 OnStep",
        observer_lat=50.336,
        observer_lon=8.533,
        observer_alt_m=0.0,
        flip_request_deg=100.0,
        tracking_stop_deg=110.0,
        flip_allowance_seconds=120.0,
        reserve_seconds=30.0,
        safe_meridian_flip_via_home=True,
        focuser_max_position=10000,
        home_motion_enabled=True,
    )
    defaults.update(overrides)
    return IndiRuntimeConfig(**defaults)


def make_fake_onstep_indi_connection(
    config: IndiRuntimeConfig | None = None,
) -> tuple[OnStepConnection, list[FakeOnStepIndiClient]]:
    """An `OnStepConnection` whose client is a `FakeOnStepIndiClient`, for
    adapter and contract tests that need the shims but no INDI server."""
    made: list[FakeOnStepIndiClient] = []
    cfg = config or fake_indi_runtime_config()

    def factory(**kwargs: object) -> FakeOnStepIndiClient:
        client = FakeOnStepIndiClient(config=cfg)
        made.append(client)
        return client

    return OnStepConnection(cfg, client_factory=factory), made
