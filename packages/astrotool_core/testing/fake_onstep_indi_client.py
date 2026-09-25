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
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

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


@dataclass
class FakeIndiAxisMoveResult:
    axis: str
    requested_deg: float
    measured_deg: float
    stop_confirmed: bool = True


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

    def __post_init__(self) -> None:
        self.mount = FakeIndiMount(self)

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
        """0.4.1 behavior: a plain UNPARK + TRACK_OFF switch flip, never a
        slew, and no longer requires the mount to be parked going in (an
        already-unparked mount just gets the TRACK_OFF, accepted immediately
        if tracking is already off -- OnStepAdapter#17). Does NOT touch
        `at_home`."""
        self._require_home_motion()
        if self.unpark_rejected:
            return IndiUnparkResult(False, "unpark", None, "fake rejected unpark", None)
        if self.slewing:
            return IndiUnparkResult(False, "preflight", None, "fake: slewing", None)
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
