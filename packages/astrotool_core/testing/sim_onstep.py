"""Scenario simulator for the OnStep controller (mount + focuser) behind
`OnStepConnection` (issue #51).

Plugs in exactly where production does: `OnStepConnection(client_factory=...)`
gets a `SimulatedOnStepIndiClient` instead of `onstep_adapter.OnStepIndiClient`,
so the production shims (`OnStepFocuserAdapter`, `OnStepMountParkAdapter`,
`OnStepMountPulseAdapter`) and `OnStepConnection` itself run unchanged. The
simulated client extends `FakeOnStepIndiClient` (whose default behaviour the
existing tests rely on) and reproduces what OnStepAdapter 0.4.1 itself does
on top of the controller -- the semantics the application really sees --
on an injected `astrotool_core.timing.Clock` (a `FakeClock` in tests), never
with real sleeps. Source references are to the published 0.4.1 wheel
(`site-packages/onstep_adapter/` before S6.0e). S6.0e pinned 0.5.0, which leaves
indi_axis_motion, indi_tracking, indi_stop, indi_home, indi_focuser, indi_meridian,
indi_config and meridian_policy byte-identical; in indi_client.py the 0.4.1 line numbers
cited below shift (0.5.0 adds the guide controller): +1 from :12, +2 from :83, +3 from :139,
+9 from :195, +18 from :234, +19 from :371; indi_status.py gains only the `guiding` snapshot
field (+1 from :41, +2 from :152). Everything
modelled here except the guide pulse is therefore unchanged in 0.5.0:

- **connect error variants** (`OnStepScenario.connect_errors`): each
  `connect()` raises the next queued exception (a bare exception, or a
  `ConnectFailure` to say where it arises). Errors inside 0.4.1's connect
  `try` close the client's own transport before re-raising
  (indi_client.py:208-210: ConnectionError/KeyError/RuntimeError/TimeoutError/
  ValueError) -- recorded as `self_closes`, separately from `close_calls`
  (calls of `close()` by OnStepConnection), so a test can prove
  `OnStepConnection.acquire()` still closes a failed client itself. The
  "already connected" RuntimeError is raised BEFORE that `try` (:127-128)
  and does not self-close (`ConnectFailure(..., self_closes=False)`). Real
  sources: TimeoutError (a property never arrived, `wait_property`),
  ConnectionError (CONNECT not On, :142-143), RuntimeError (already
  connected :127-128, Safe-flip/guard checks :156-171), ValueError
  (unparseable TIME_UTC, :146), KeyError (a missing UTC/East/West element,
  :146, :173).
- **park / unpark**: unpark always ends with tracking OFF -- 0.4.1 issues
  TRACK_OFF itself and confirms not-tracking from fresh status
  (indi_home.py:174-184), so no firmware tracking side effect is visible above
  the adapter and none is offered here.
- **tracking enable** (`tracking_confirm_delay_s`), modeled on
  `enable_tracking_via_indi` (indi_tracking.py): the strict-authority preflight
  (:57-66: parked/slewing/at_limit/not live, and under "strict" also at_home,
  no home or time/site authority, coordinates not live) refuses with
  (False, False); already tracking is (False, True) (:75-79); otherwise
  TRACK_ON is sent, then the loop (:89-110) repeats: deadline check
  (`now >= deadline` -> TimeoutError), sleep 0.15 s, observe; it confirms on
  the 2nd consecutive poll that sees tracking. With tracking on `d` seconds
  after the command, poll k (at k * 0.15) sees it from k1 = max(1,
  ceil(d / 0.15)); confirmation is at (k1 + 1) * 0.15 and needs the check
  before that poll, at k1 * 0.15, to be before the deadline. Otherwise the
  TimeoutError comes at the first check at or after the deadline, and the
  adapter calls `emergency_stop()` (:115-117) -- the END STATE IS TRACKING
  OFF, result (accepted=True, confirmed=False, "Tracking ON was not
  confirmed ...").
- **stale status** (`onstep_status_not_fresh` / `coordinates_not_fresh`,
  `status_live=False`) and an axis move refusing on it, as 0.4.1's
  `IndiAxisMover._check` does (indi_axis_motion.py).
- **axis moves**: 0.4.1 argument validation (offset 30"..10 deg, timeout
  1..60 s, poll 0.05..0.25 s), `|offset| / axis_rate_deg_per_s` of clock time,
  queued **rejections** (`axis_move_errors`) raised after the move was
  issued -- like 0.4.1's in-motion failures, they trigger `emergency_stop()`
  (indi_axis_motion.py:246-249 `except BaseException: if issued: emergency_stop()`);
  "Another axis motion is active" (retryable) is raised before issuing.
- a focuser modeled on 0.4.1 `IndiFocuser` (indi_focuser.py): integer target
  and positive finite timeout (ValueError otherwise, :79-82); refusal text and
  0.4.1 blocker names (:42-77); travel limit = min(driver, configured) (:91-93);
  a target equal to the position is an immediate success (:94-95); a driver
  Alert -> "Focuser position update is invalid" + stop (:121-135); the
  reported position moves during travel (interpolated on the clock); a
  **Busy forever** move times out after `timeout` and then also spends the
  5 s internal stop wait (:130-135, `_stop_unlocked(timeout=5.0)`) without
  confirmation; a **backlash** model (`optical_position`); stop.
- **concurrent status reads during a compound operation**: every compound
  operation (focuser move, axis move, park, unpark) and every status read is
  reported to a `CompoundOperationMonitor`, which records each status read
  that arrived from another thread while a compound operation was active
  (`interleavings`) -- the 7b21bdf1 hazard 9cea2e9 closed.

- **stop during a blocking axis move** (S6.0c, from the 0.4.1 source):
  `emergency_stop` may be called from any thread while `move_*_axis_deg`
  blocks -- 0.4.1's own meridian supervisor does exactly that from its
  thread (indi_meridian.py:113-115, 122-125), and `stop_mount_via_indi`
  takes none of `IndiAxisMover`'s locks (indi_stop.py:22-88 vs
  indi_axis_motion.py:64, 134). It sends ABORT (+ TRACK_OFF) first
  (indi_stop.py:37-50), so the mount stops short of the target at once, then
  waits for two fresh stopped status samples (:52-80) --
  `stop_confirm_latency_s` of clock time here. The blocked move has NO
  cancel hook (indi_axis_motion.py:108-112): its poll loop never sees the
  target (:184-208) and runs on to its own deadline (`timeout_s` after the
  goto, :164, :170-171), raises TimeoutError, and on the way out calls
  `emergency_stop()` once more (:246-248). `axis_moves_aborted` counts moves
  a stop cut short.

- **0.5.0 guide pulses** (S6.0d; since S6.0e -- 0.5.0 pinned -- the default
  `OnStepScenario(guide_pulses=GuidePulseScenario())`; `guide_pulses=None` is the published 0.4.1
  surface, no `guide_pulse` on the facade -- pair it with `simulate_onstep_adapter_041_package`).
  The semantics are `FakeOnStepIndiClient.guide_pulse`'s, modelled on the published 0.5.0 wheel
  (see that module's docstring, cited per line); here each issued chunk also takes its length of
  clock time (0.5.0 paces every chunk to its duration, indi_guiding.py:205-220), so a Stop or a
  meridian-phase change can arrive during a chunk via `clock.call_later` -- 0.5.0's post-chunk
  status wait then refuses and stops the mount (indi_guiding.py:282-303). A post-chunk wait that
  expires (`GuidePulseScenario.idle_timeouts`) takes the command timeout of clock time.
  Whether the real driver/firmware ends a running guide timer on ABORT is not known (field
  item): a chunk in flight here always runs to its end.

Deliberately NOT modeled (unknown or owned elsewhere): whether the real
controller/driver honours INDI ABORT mid-GOTO exactly like this (field
only), meridian phase/limit geometry, the
`focuser_property_alert` blocker (whether the driver leaves ABS_FOCUS_POSITION
in Alert after a rejection is driver behaviour), and the INDI wire protocol
underneath `OnStepIndiClient` (OnStepConnection never sees it).
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from astrotool_core.onstep.connection import OnStepConnection
from astrotool_core.testing.fake_onstep_indi_client import (
    FakeIndiAxisMoveResult,
    FakeIndiFocuser,
    FakeIndiGuidePulseResult,
    FakeIndiTrackingResult,
    FakeOnStepIndiClient,
    IndiFocuserMoveResult,
    IndiFocuserSnapshot,
    IndiMountSnapshot,
    IndiPositionResult,
    IndiRuntimeConfig,
    IndiStartupStatus,
    IndiStopResult,
    IndiUnparkResult,
    fake_indi_runtime_config,
    simulate_onstep_adapter_041_package,
)
from astrotool_core.timing import SYSTEM_CLOCK, Clock

#: 0.4.1 `IndiAxisMover.move` argument bounds (indi_axis_motion.py).
_MIN_AXIS_MOVE_DEG = 30.0 / 3600.0
_MAX_AXIS_MOVE_DEG = 10.0
#: 0.4.1 `enable_tracking_via_indi` poll period and required fresh polls.
_TRACKING_POLL_S = 0.15
#: Float slack for poll-count arithmetic (k * 0.15 is not exact in binary).
_EPS = 1e-9
#: 0.4.1 `IndiFocuser.move_absolute`: the stop wait after a failed move.
_FOCUSER_STOP_WAIT_S = 5.0

#: Retryable: 0.4.1 raises this when its own axis lock is already held.
AXIS_BUSY = "Another axis motion is active"
#: Non-retryable: parked / tracking / slewing / at limit.
AXIS_STATE_REFUSED = (
    "Local axis motion requires fresh unparked, stationary, non-tracking and fault-free state"
)
TRACKING_NOT_CONFIRMED = "Tracking ON was not confirmed by fresh OnStep status"

__all__ = [
    "AXIS_BUSY",
    "AXIS_STATE_REFUSED",
    "TRACKING_NOT_CONFIRMED",
    "CompoundOperationMonitor",
    "ConnectFailure",
    "FocuserScenario",
    "GuidePulseScenario",
    "ObservableRLock",
    "OnStepScenario",
    "SimulatedIndiFocuser",
    "SimulatedOnStepIndiClient",
    "install_observable_operation_lock",
    "make_simulated_onstep_connection",
    "simulate_onstep_adapter_041_package",
]


class CompoundOperationMonitor:
    """Records how status reads and compound operations overlap in time.

    `interleavings` gets one `(active_operation, intruder)` entry whenever a
    status read or another compound operation starts on a thread *other* than
    the one inside an active compound operation. Same-thread nesting (an
    adapter method reading status inside its own move) is legitimate and not
    recorded. `events` is the plain begin/end/status order, for assertions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[int, str] = {}
        self.interleavings: list[tuple[str, str]] = []
        self.events: list[str] = []

    def _intrusion(self, name: str) -> None:
        me = threading.get_ident()
        for thread, operation in self._active.items():
            if thread != me:
                self.interleavings.append((operation, name))
                return

    @contextmanager
    def compound(self, name: str) -> Iterator[None]:
        me = threading.get_ident()
        with self._lock:
            self._intrusion(name)
            nested = me in self._active
            if not nested:
                self._active[me] = name
            self.events.append(f"begin {name}")
        try:
            yield
        finally:
            with self._lock:
                if not nested:
                    self._active.pop(me, None)
                self.events.append(f"end {name}")

    def observe(self, name: str) -> None:
        with self._lock:
            self._intrusion(name)
            self.events.append(name)

    @property
    def active(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._active.values())


@dataclass(frozen=True)
class ConnectFailure:
    """A queued connect failure. `self_closes`: the error arises inside 0.4.1's
    connect `try`, which closes the client's own transport (indi_client.py:208-210);
    False for the "already connected" check before it (:127-128)."""

    error: BaseException
    self_closes: bool = True


@dataclass
class FocuserScenario:
    """The simulated focuser. Limits are the driver's FOCUS_MAX and the
    configured maximum (0.4.1 refuses a target above `min` of both)."""

    position: int | None = 5000
    driver_maximum: int | None = 10000
    configured_maximum: int | None = 10000
    steps_per_s: float = 1000.0
    #: Mechanical play: after a direction reversal the first `backlash_steps`
    #: of motor travel do not move the optics (`optical_position`).
    backlash_steps: int = 0
    #: The driver never leaves Busy: a move blocks until its timeout plus
    #: 0.4.1's 5 s stop wait, then reports "Focuser target was not
    #: confirmed" with the stop unconfirmed; status stays moving.
    busy_forever: bool = False
    #: The driver answers a move with an Alert -> 0.4.1's error text.
    reject_moves: bool = False
    #: The driver stopped republishing ABS_FOCUS_POSITION -> `focuser_position_stale`.
    position_stale: bool = False
    connected: bool = True


@dataclass
class GuidePulseScenario:
    """The simulated 0.5.0 guide-pulse controller (opt-in, see the module docstring)."""

    #: The controller's guide rate (x sidereal) -- 0.5.0 does not expose it.
    rate_x: float = 0.5
    #: Per-direction override ("north"/"south"/"east"/"west"), e.g. an asymmetric RA response.
    direction_rate_x: dict[str, float] = field(default_factory=dict)
    #: The geometric meridian phase (`classify_meridian`'s HA-based result).
    meridian_phase: str = "pre_meridian_allowed"
    #: Further status-reader blockers seen by the preflight (e.g. "pier_side_unknown").
    extra_blockers: tuple[str, ...] = ()
    #: Raised by the next issued chunks' completion waits (None = completes); shared list.
    chunk_errors: list[BaseException | None] = field(default_factory=list)
    #: Per completed chunk: True = OnStep never publishes a fresh `G`-cleared status, so 0.5.0's
    #: post-chunk wait expires after the command timeout (TimeoutError); shared list.
    idle_timeouts: list[bool] = field(default_factory=list)


@dataclass
class OnStepScenario:
    """One configurable OnStep controller state (see the module docstring)."""

    connect_errors: list[BaseException | ConnectFailure] = field(default_factory=list)
    connect_latency_s: float = 0.0
    parked: bool = True
    tracking: bool = False
    at_home: bool = False
    home_authority: bool = True
    time_site_authority: bool = True
    unpark_latency_s: float = 0.0
    park_latency_s: float = 0.0
    #: The controller confirms TRACK_ON this long after the command.
    tracking_confirm_delay_s: float = 0.0
    #: Status/coordinates not fresh: blockers + `status_live=False`.
    status_stale: bool = False
    axis_rate_deg_per_s: float = 2.0
    #: Raised by the next axis moves (after the move was issued), one per move.
    axis_move_errors: list[BaseException] = field(default_factory=list)
    #: `emergency_stop` waits this long (clock time) for its stopped-status
    #: confirmation after sending ABORT (0.4.1: two fresh samples, <= 5 s).
    stop_confirm_latency_s: float = 0.0
    focuser: FocuserScenario = field(default_factory=FocuserScenario)
    #: OnStepAdapter 0.5.0's guide pulse on the mount facade (S6.0e: the default, as pinned);
    #: None = the published 0.4.1 facade (no API) -- see `simulate_onstep_adapter_041_package`.
    guide_pulses: GuidePulseScenario | None = field(default_factory=GuidePulseScenario)


class SimulatedIndiFocuser(FakeIndiFocuser):
    """`client.focuser`, mirroring 0.4.1 `IndiFocuser` semantics on a clock."""

    def __init__(
        self, scenario: FocuserScenario, *, clock: Clock, monitor: CompoundOperationMonitor
    ) -> None:
        super().__init__()
        self.scenario = scenario
        self._clock = clock
        self._monitor = monitor
        self.position = scenario.position
        self.driver_maximum = scenario.driver_maximum
        self.configured_maximum = scenario.configured_maximum
        self.connected = scenario.connected
        self.reject_moves = scenario.reject_moves
        self.optical_position: float | None = (
            float(scenario.position) if scenario.position is not None else None
        )
        self.move_log: list[int] = []
        self._motion_lock = threading.Lock()
        self._cancel = threading.Event()
        #: (start time, from, to) of the travel in progress, for the
        #: position the driver reports while moving.
        self._travel: tuple[float, int, int] | None = None

    def _reported_position(self) -> int | None:
        if self._travel is None or self.scenario.busy_forever:
            return self.position
        started, start, target = self._travel
        done = (self._clock.monotonic() - started) * self.scenario.steps_per_s
        step = min(done, abs(target - start))
        return int(start + math.copysign(step, target - start))

    def _snapshot(self) -> IndiFocuserSnapshot:
        """0.4.1's blocker computation (indi_focuser.py `get_status`)."""
        base = FakeIndiFocuser.get_status(self)
        position, maximum = self._reported_position(), self.driver_maximum
        blockers: list[str] = [] if self.connected else ["indi_device_disconnected"]
        if position is None or position < 0:
            blockers.append("focuser_position_unknown")
        if maximum is None or maximum <= 0:
            blockers.append("focuser_maximum_unknown")
        if maximum is not None and position is not None and position > maximum:
            blockers.append("focuser_limit_conflicts_with_position")
        if self.configured_maximum is None:
            blockers.append("focuser_configured_maximum_missing")
        elif position is not None and position > self.configured_maximum:
            blockers.append("focuser_position_exceeds_configuration")
        if self.scenario.position_stale:
            blockers.append("focuser_position_stale")
        return replace(
            base,
            position=position,
            moving=self.moving,
            move_ready=not blockers and not self.moving,
            blockers=tuple(blockers),
        )

    def get_status(self) -> IndiFocuserSnapshot:
        self._monitor.observe("focuser.status")
        return self._snapshot()

    def _move_optics(self, target: int) -> None:
        """Moves the optics through the backlash dead band to `target`."""
        if self.optical_position is None:
            self.optical_position = float(target)
            return
        play = float(self.scenario.backlash_steps)
        self.optical_position = min(max(self.optical_position, target - play), float(target))

    def move_absolute(self, target: int, *, timeout: float = 30.0) -> IndiFocuserMoveResult:
        if type(target) is not int:
            raise ValueError("Focuser target must be an integer")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Focuser timeout must be positive and finite")
        with self._monitor.compound("focuser.move"), self._motion_lock:
            self._cancel.clear()
            before = self._snapshot()
            if not before.move_ready:
                blockers = ", ".join(before.blockers)
                return IndiFocuserMoveResult(
                    target, False, before.position, None, f"Focuser move refused: {blockers}"
                )
            limits = [m for m in (self.driver_maximum, self.configured_maximum) if m is not None]
            if target < 0 or target > min(limits):
                raise ValueError("Focuser target exceeds confirmed travel limits")
            start = self.position or 0
            if target == start:
                return IndiFocuserMoveResult(target, True, target, None, None)
            if self.reject_moves:
                return IndiFocuserMoveResult(
                    target, False, None, True, "Focuser position update is invalid"
                )
            self.move_log.append(target)
            self.moving = True
            if self.scenario.busy_forever:
                cancelled = not self._clock.sleep(timeout, self._cancel)
                self._clock.sleep(_FOCUSER_STOP_WAIT_S)  # 0.4.1's stop wait, never confirmed
                error = (
                    "Focuser move cancelled" if cancelled else "Focuser target was not confirmed"
                )
                return IndiFocuserMoveResult(target, False, None, False, error)
            self._travel = (self._clock.monotonic(), start, target)
            try:
                completed = self._clock.sleep(
                    abs(target - start) / self.scenario.steps_per_s, self._cancel
                )
                reached = self._reported_position()
            finally:
                self._travel = None
            if not completed:
                self.position = reached
                self.moving = False
                return IndiFocuserMoveResult(target, False, None, True, "Focuser move cancelled")
            self.position = target
            self._move_optics(target)
            self.moving = False
            return IndiFocuserMoveResult(target, True, target, None, None)

    def stop(self, *, timeout: float = 5.0) -> bool:
        self.stop_calls += 1
        self._cancel.set()
        if self.scenario.busy_forever:
            return False  # the abort is never confirmed: the driver stays Busy
        if self._travel is None:
            self.moving = False
        return True


@dataclass
class SimulatedOnStepIndiClient(FakeOnStepIndiClient):
    """`OnStepIndiClient` stand-in driven by an `OnStepScenario` on a clock."""

    scenario: OnStepScenario = field(default_factory=OnStepScenario)
    clock: Clock = field(default=SYSTEM_CLOCK)
    monitor: CompoundOperationMonitor = field(default_factory=CompoundOperationMonitor)
    focuser: SimulatedIndiFocuser = field(init=False)

    def __post_init__(self) -> None:
        guide = self.scenario.guide_pulses
        # Before the facade is built (FakeOnStepIndiClient.__post_init__).
        self.guide_pulse_api = guide is not None
        if guide is not None:
            self.guide_rate_x = guide.rate_x
            self.guide_direction_rate_x = guide.direction_rate_x
            self.meridian_phase = guide.meridian_phase
            self.guide_blockers = guide.extra_blockers
            self.guide_chunk_errors = guide.chunk_errors
            self.guide_idle_timeouts = guide.idle_timeouts
        super().__post_init__()
        s = self.scenario
        self.parked, self.tracking, self.at_home = s.parked, s.tracking, s.at_home
        self.home_authority_established = s.home_authority
        self.time_site_authority = s.time_site_authority
        self.focuser = SimulatedIndiFocuser(s.focuser, clock=self.clock, monitor=self.monitor)
        self.tracking_enable_calls = 0
        self.emergency_stop_calls = 0
        #: `close()` calls from outside (OnStepConnection) vs. 0.4.1's own close
        #: of its transport inside a failed connect.
        self.close_calls = 0
        self.self_closes = 0
        #: Set by `emergency_stop` while an axis move is in motion (see module docstring).
        self._axis_abort = threading.Event()
        self._axis_in_motion = False
        self.axis_moves_aborted = 0

    # ---- connection lifecycle -----------------------------------------
    def connect(self, *, timeout: float = 5.0) -> IndiStartupStatus:
        if self.scenario.connect_latency_s:
            self.clock.sleep(min(self.scenario.connect_latency_s, timeout))
        if self.scenario.connect_errors:
            self.connect_calls += 1
            queued = self.scenario.connect_errors.pop(0)
            failure = queued if isinstance(queued, ConnectFailure) else ConnectFailure(queued)
            if failure.self_closes:
                self.self_closes += 1  # indi_client.py:208-210 closes before re-raising
                self.closed = True
            else:
                self.closed = False  # raised before the try: the client stays as it was
            raise failure.error
        return super().connect(timeout=timeout)

    def close(self) -> None:
        self.close_calls += 1
        super().close()

    # ---- status ----------------------------------------------------------
    def observe_mount(self) -> IndiMountSnapshot:
        self.monitor.observe("mount.status")
        snapshot = super().observe_mount()
        if not self.scenario.status_stale:
            return snapshot
        return replace(
            snapshot,
            status_live=False,
            coordinates_live=False,
            status_age_ms=10_000.0,
            coordinates_age_ms=10_000.0,
            blockers=("onstep_status_not_fresh", "coordinates_not_fresh"),
        )

    # ---- park / unpark / stop / tracking -----------------------------------
    def unpark(self, *, timeout: float = 20.0) -> IndiUnparkResult:
        with self.monitor.compound("mount.unpark"):
            self.clock.sleep(self.scenario.unpark_latency_s)
            return super().unpark(timeout=timeout)

    def park(self, *, timeout: float = 120.0) -> IndiPositionResult:
        with self.monitor.compound("mount.park"):
            self.clock.sleep(self.scenario.park_latency_s)
            return super().park(timeout=timeout)

    def emergency_stop(self, *, timeout: float = 5.0) -> IndiStopResult:
        """ABORT first (an axis move in motion stops short at once), then the
        confirmation wait (indi_stop.py:37-80; see module docstring)."""
        self.emergency_stop_calls += 1
        if self._axis_in_motion:
            self._axis_abort.set()
        result = super().emergency_stop(timeout=timeout)
        self.clock.sleep(min(self.scenario.stop_confirm_latency_s, timeout))
        return result

    def enable_tracking(self, *, timeout: float = 8.0) -> FakeIndiTrackingResult:
        """indi_tracking.py `enable_tracking_via_indi` (see module docstring)."""
        self.tracking_enable_calls += 1
        snapshot = self.observe_mount()
        strict = self.config.tracking_authority_policy == "strict" and (
            snapshot.at_home
            or not snapshot.home_authority
            or not snapshot.time_site_authority
            or not snapshot.coordinates_live
        )
        hard = not snapshot.status_live or snapshot.parked or snapshot.slewing or snapshot.at_limit
        if hard or strict:
            return FakeIndiTrackingResult(False, False, "tracking preflight refused")
        if snapshot.tracking:
            return FakeIndiTrackingResult(False, True, None)
        # indi_tracking.py:85-117, poll by poll (see module docstring).
        k1 = max(1, math.ceil(self.scenario.tracking_confirm_delay_s / _TRACKING_POLL_S - _EPS))
        if not self.tracking_rejected and k1 < timeout / _TRACKING_POLL_S - _EPS:
            confirm_after = (k1 + 1) * _TRACKING_POLL_S  # the check before it beat the deadline
        else:
            # TimeoutError at the first pre-poll check at or after the deadline.
            self.clock.sleep(math.ceil(timeout / _TRACKING_POLL_S - _EPS) * _TRACKING_POLL_S)
            self.emergency_stop()  # indi_tracking.py:115-117: TRACK_OFF, end state OFF
            return FakeIndiTrackingResult(True, False, TRACKING_NOT_CONFIRMED)
        self.clock.sleep(confirm_after)
        self.tracking = True
        return FakeIndiTrackingResult(True, True, None)

    # ---- 0.5.0 guide pulses -------------------------------------------------
    def guide_pulse(
        self, direction: str, duration_ms: int, *, command_timeout: float = 3.0
    ) -> FakeIndiGuidePulseResult:
        with self.monitor.compound("mount.guide_pulse"):
            return super().guide_pulse(direction, duration_ms, command_timeout=command_timeout)

    def _guide_chunk(self, direction: str, chunk_ms: int) -> None:
        self.clock.sleep(chunk_ms / 1000.0)  # the controller's guide timer
        super()._guide_chunk(direction, chunk_ms)

    def _guide_idle_wait_elapse(self, seconds: float) -> None:
        self.clock.sleep(seconds)  # 0.5.0 polls until its post-chunk deadline

    # ---- axis motion -------------------------------------------------------
    def move_axis_deg(
        self, axis: str, offset_deg: float, *, timeout_s: float = 30.0, poll_s: float = 0.1
    ) -> FakeIndiAxisMoveResult:
        if axis not in ("ra", "dec"):
            raise ValueError("axis must be ra or dec")
        if not math.isfinite(offset_deg) or not (
            _MIN_AXIS_MOVE_DEG <= abs(offset_deg) <= _MAX_AXIS_MOVE_DEG
        ):
            raise ValueError("Axis move must be between 30 arcseconds and 10 degrees")
        if not math.isfinite(timeout_s) or not 1 <= timeout_s <= 60:
            raise ValueError("Axis move timeout must be between 1 and 60 seconds")
        if not math.isfinite(poll_s) or not 0.05 <= poll_s <= 0.25:
            raise ValueError("Axis move poll interval must be 0.05..0.25 seconds")
        if not self._axis_lock.acquire(blocking=False):
            raise RuntimeError(AXIS_BUSY)
        try:
            with self.monitor.compound("mount.axis_move"):
                if self.scenario.status_stale:
                    raise RuntimeError(
                        "Axis motion safety inputs unavailable: "
                        "['coordinates_not_fresh', 'onstep_status_not_fresh']"
                    )
                if self.tracking or self.slewing or self.parked or self.at_limit:
                    raise RuntimeError(AXIS_STATE_REFUSED)
                self.axis_move_calls.append((axis, offset_deg))
                duration = abs(offset_deg) / self.scenario.axis_rate_deg_per_s
                if self.scenario.axis_move_errors:
                    # A failure after the goto was issued: 0.4.1 stops the mount.
                    self.clock.sleep(min(duration, timeout_s))
                    self.emergency_stop()
                    raise self.scenario.axis_move_errors.pop(0)
                if duration > timeout_s:
                    self.clock.sleep(timeout_s)
                    self.emergency_stop()
                    raise TimeoutError("Axis move did not reach its finite target")
                started = self.clock.monotonic()
                self._axis_abort.clear()
                self.slewing = True
                self._axis_in_motion = True
                try:
                    arrived = self.clock.sleep(duration, self._axis_abort)
                finally:
                    self._axis_in_motion = False
                    self.slewing = False
                travelled = offset_deg
                if not arrived:
                    # Stopped short; 0.4.1 polls on to its own deadline, then
                    # raises and stops once more (indi_axis_motion.py:170-171, :246-248).
                    self.axis_moves_aborted += 1
                    elapsed = self.clock.monotonic() - started
                    travelled = offset_deg * min(1.0, elapsed / duration)
                    self._shift(axis, travelled)
                    self.clock.sleep(max(0.0, started + timeout_s - self.clock.monotonic()))
                    self.emergency_stop()
                    raise TimeoutError("Axis move did not reach its finite target")
                self._shift(axis, travelled)
                self.tracking = False
                return FakeIndiAxisMoveResult(axis, offset_deg, offset_deg)
        finally:
            self._axis_lock.release()

    def _shift(self, axis: str, offset_deg: float) -> None:
        if axis == "ra":
            self.ha_deg += offset_deg
        else:
            self.dec_deg += offset_deg


class ObservableRLock:
    """A re-entrant lock that tells a test which threads are blocked on it.

    Replaces `OnStepConnection.operation_lock` (same RLock semantics) so a
    test can wait -- deterministically, as a barrier -- until a poller thread
    is provably blocked behind a compound operation, instead of sleeping and
    hoping the scheduler interleaved the threads."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cond = threading.Condition()
        self._waiting: set[int] = set()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(blocking=False):
            return True
        if not blocking:
            return False
        me = threading.get_ident()
        with self._cond:
            self._waiting.add(me)
            self._cond.notify_all()
        try:
            return self._lock.acquire(True, timeout)
        finally:
            with self._cond:
                self._waiting.discard(me)
                self._cond.notify_all()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    def wait_until_blocked_or_done(
        self, thread: threading.Thread, *, timeout_s: float = 5.0
    ) -> str:
        """Barrier: returns "blocked" once `thread` waits on this lock, "done"
        once it has finished, "timeout" if neither happens within `timeout_s`
        (real time -- a safety bound, not a policy wait)."""

        def state() -> str | None:
            if thread.ident in self._waiting:
                return "blocked"
            if not thread.is_alive():
                return "done"
            return None

        with self._cond:
            if not self._cond.wait_for(lambda: state() is not None, timeout_s):
                return "timeout"
            result = state()
        return result or "timeout"


def install_observable_operation_lock(connection: OnStepConnection) -> ObservableRLock:
    """Swaps `connection.operation_lock` for an `ObservableRLock` (before any
    adapter operation starts). Harmless on code without the lock attribute
    (a revert experiment): nothing then uses it, which is what such a test
    must detect."""
    lock = ObservableRLock()
    connection.operation_lock = lock  # type: ignore[assignment]
    return lock


def make_simulated_onstep_connection(
    scenario: OnStepScenario | None = None,
    *,
    clock: Clock | None = None,
    config: IndiRuntimeConfig | None = None,
    timeout: float = 5.0,
) -> tuple[OnStepConnection, list[SimulatedOnStepIndiClient]]:
    """A production `OnStepConnection` whose client is a
    `SimulatedOnStepIndiClient` -- one per `connect()` the connection makes
    (a failed connect leaves its client in the list too, closed). Later
    clients continue the same controller state (one physical controller)."""
    made: list[SimulatedOnStepIndiClient] = []
    cfg = config or fake_indi_runtime_config()
    the_scenario = scenario or OnStepScenario()
    the_clock = clock or SYSTEM_CLOCK
    monitor = CompoundOperationMonitor()

    def factory(**_kwargs: object) -> SimulatedOnStepIndiClient:
        client = SimulatedOnStepIndiClient(
            config=cfg, scenario=the_scenario, clock=the_clock, monitor=monitor
        )
        if made:
            previous = made[-1]
            client.focuser = previous.focuser
            client.parked, client.tracking = previous.parked, previous.tracking
            client.at_home, client.ha_deg, client.dec_deg = (
                previous.at_home,
                previous.ha_deg,
                previous.dec_deg,
            )
        made.append(client)
        return client

    return OnStepConnection(cfg, timeout=timeout, client_factory=factory), made
