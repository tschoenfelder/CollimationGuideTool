"""S6.8a (R3/R1/R4/F12): the one mount-motion executor and its callers' cancellation.

- one in-flight mount motion app-wide: a second claim is refused with a busy reason;
- `abort()` cancels the claim in flight and sends the mount's lock-free Stop, `abort(owner)`
  only the named workflow's motion;
- the tracking rule: an axis move never stops tracking -- a tracking mount is refused with
  the mode's reason;
- fine-collimation Cancel during a reacquisition GOTO: the GOTO is stopped, the settle ends
  at once and the second axis of the plan is never sent (before: Cancel was only checked
  between corrections, the settle always ran to its end, the mount was never stopped).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.operating_mode import (
    ASTRONOMICAL_TRACKING_AXIS_MOVE_REFUSED,
    TERRESTRIAL_TRACKING_AXIS_MOVE_REFUSED,
    TERRESTRIAL_TRACKING_REPAIR_FAILED,
    OperatingMode,
    TrackingEnforcer,
)
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.timing import FakeClock
from collimation_tool.application.mount_motion import (
    MOTION_BUSY,
    MotionClaim,
    MountMotionExecutor,
)
from collimation_tool.application.recenter_policy import CollimationRecenterPolicy, RecenterConfig
from collimation_tool.application.star_acquisition import REACQUISITION_OWNER

_SIGN = {AxisDirection.POSITIVE: 1.0, AxisDirection.NEGATIVE: -1.0}


def _calibration(px_per_ms: float = 0.1) -> CalibrationMatrix:
    responses = {}
    for axis in MountAxis:
        for direction in AxisDirection:
            delta = 100 * px_per_ms * _SIGN[direction]
            dx, dy = (delta, 0.0) if axis is MountAxis.AXIS1 else (0.0, delta)
            responses[(axis, direction)] = AxisResponse(
                axis=axis,
                direction=direction,
                duration_ms=100,
                dx_px=dx,
                dy_px=dy,
                px_per_ms=px_per_ms,
            )
    return CalibrationMatrix(responses=responses)


class _AngularMount:
    """Angular-only mount with the adapter's Stop latch (abort / clear_abort)."""

    def __init__(self) -> None:
        self.moves: list[tuple[MountAxis, AxisDirection, float]] = []
        self.abort_count = 0
        self.clear_count = 0
        self.during_move: list[object] = []

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            supports_pulse_guiding=False,
            min_pulse_ms=0,
            max_pulse_ms=0,
            supports_angular_moves=True,
            min_angular_arcsec=0.0,
        )

    def status(self) -> MountStatus:
        return MountStatus(connected=True, tracking=False, slewing=False)

    def pulse_axis(self, *_args: object, **_kwargs: object) -> CommandResult:
        raise AssertionError("no timed pulses on this mount")

    def installed_rate(self, _axis: MountAxis, _direction: AxisDirection) -> float | None:
        return None

    def install_rate(self, *_args: object) -> None:
        pass

    def move_angular(
        self, axis: MountAxis, direction: AxisDirection, arcsec: float
    ) -> CommandResult:
        self.moves.append((axis, direction, arcsec))
        for action in self.during_move:
            action()  # type: ignore[operator]
        self.during_move = []
        return CommandResult(accepted=True)

    def abort(self) -> None:
        self.abort_count += 1

    def clear_abort(self) -> None:
        self.clear_count += 1


def _locked(x: float, y: float) -> TrackingResult:
    return TrackingResult(state=TrackingState.LOCKED, x=x, y=y, matched_source=None)


class TestOneMotionInFlight:
    def test_a_second_claim_is_refused_with_the_busy_reason(self) -> None:
        mount = _AngularMount()
        executor = MountMotionExecutor(mount)
        first = executor.claim("Mount Align")
        assert not isinstance(first, str)

        second = executor.claim("the Mount panel's park")

        assert isinstance(second, str)
        assert second.startswith(MOTION_BUSY) and "Mount Align is moving the mount" in second
        assert "nothing was sent" in second
        assert mount.clear_count == 1  # only the accepted claim re-armed the Stop latch
        executor.release(first)
        assert not isinstance(executor.claim("the Mount panel's park"), str)

    def test_a_stale_release_never_frees_a_newer_claim(self) -> None:
        executor = MountMotionExecutor()
        first = executor.claim("a")
        assert not isinstance(first, str)
        executor.release(first)
        second = executor.claim("b")
        assert not isinstance(second, str)
        executor.release(first)
        assert executor.in_flight == "b"


class TestAbort:
    def test_abort_cancels_the_claim_and_stops_the_mount_once(self) -> None:
        mount = _AngularMount()
        executor = MountMotionExecutor(mount)
        claim = executor.claim("Mount Align", mount)
        assert not isinstance(claim, str)

        executor.abort()

        assert claim.cancelled
        assert mount.abort_count == 1

    def test_an_owner_scoped_abort_leaves_another_workflows_motion_alone(self) -> None:
        mount = _AngularMount()
        executor = MountMotionExecutor(mount)
        claim = executor.claim("Mount Align")
        assert not isinstance(claim, str)

        executor.abort(REACQUISITION_OWNER)

        assert not claim.cancelled and mount.abort_count == 0

    def test_the_settle_ends_at_the_stop(self) -> None:
        clock = FakeClock()
        executor = MountMotionExecutor(clock=clock)
        claim = executor.claim("x")
        assert not isinstance(claim, str)
        clock.call_at(0.2, executor.abort)

        assert executor.settle(claim, 0.75) is False
        assert clock.monotonic() == 0.2


class TestTrackingRule:
    def _executor(self, mode: OperatingMode) -> tuple[MountMotionExecutor, TrackingEnforcer]:
        park = FakeMountPark(start_parked=False)
        enforcer = TrackingEnforcer(park, mode, settle_timeout_s=0, clock=FakeClock())
        return MountMotionExecutor(tracking_enforcer=enforcer), enforcer

    def test_astronomical_tracking_refuses_the_axis_move(self) -> None:
        executor, enforcer = self._executor(OperatingMode.ASTRONOMICAL)
        gate = executor.axis_move_gate(True, "mount_align_move")
        assert not gate.allowed and gate.reason == ASTRONOMICAL_TRACKING_AXIS_MOVE_REFUSED
        assert enforcer.evidence()["transitions"][-1]["context"] == "mount_align_move"

    def test_terrestrial_tracking_is_reported_not_stopped(self) -> None:
        executor, _ = self._executor(OperatingMode.TERRESTRIAL)
        gate = executor.axis_move_gate(True, "mount_align_move")
        assert not gate.allowed and gate.reason == TERRESTRIAL_TRACKING_AXIS_MOVE_REFUSED

    def test_a_non_tracking_mount_may_move_in_either_mode(self) -> None:
        for mode in OperatingMode:
            executor, _ = self._executor(mode)
            assert executor.axis_move_gate(False, "mount_align_move").allowed

    def test_an_unknown_tracking_state_fails_closed(self) -> None:
        assert not MountMotionExecutor().axis_move_gate(None, "x").allowed

    def test_the_unpark_enforcement_is_recorded_under_unpark(self) -> None:
        executor, enforcer = self._executor(OperatingMode.TERRESTRIAL)
        gate = executor.enforce_after_unpark()
        assert gate is not None and gate.allowed
        assert enforcer.evidence()["transitions"][-1]["context"] == "unpark"


class TestReacquisitionCancel:
    """R4: fine-collimation Cancel -> `executor.abort(REACQUISITION_OWNER)`."""

    def _policy(
        self, mount: _AngularMount, clock: FakeClock, executor: MountMotionExecutor
    ) -> tuple[CollimationRecenterPolicy, object]:
        claim = executor.claim(REACQUISITION_OWNER, mount)
        assert not isinstance(claim, str)
        policy = CollimationRecenterPolicy(
            mount,
            _calibration(),
            RecenterConfig(settle_ms=750),
            clock=clock,
            center_rate_x=8.0,
            motion_claim=claim,
        )
        return policy, claim

    def test_cancel_during_the_settle_ends_it_and_sends_no_second_axis(self) -> None:
        clock = FakeClock()
        mount = _AngularMount()
        executor = MountMotionExecutor(mount, clock=clock)
        policy, _claim = self._policy(mount, clock, executor)
        clock.call_at(0.3, lambda: executor.abort(REACQUISITION_OWNER))

        result = policy.center(lambda: _locked(140.0, 130.0), (100.0, 100.0))

        assert result.reason == "cancelled"
        assert len(mount.moves) == 1, "the second axis of the plan was still sent"
        assert clock.monotonic() == 0.3, "the settle ran on after Cancel"
        assert mount.abort_count == 1

    def test_cancel_during_the_goto_stops_the_mount_and_skips_the_settle(self) -> None:
        clock = FakeClock()
        mount = _AngularMount()
        executor = MountMotionExecutor(mount, clock=clock)
        policy, _claim = self._policy(mount, clock, executor)
        mount.during_move = [lambda: executor.abort(REACQUISITION_OWNER)]

        result = policy.center(lambda: _locked(140.0, 130.0), (100.0, 100.0))

        assert result.reason == "cancelled"
        assert mount.abort_count == 1, "Cancel did not stop the running GOTO"
        assert len(mount.moves) == 1
        assert clock.sleeps == []

    def test_a_cancel_check_between_the_two_axes_of_one_plan_is_honoured(self) -> None:
        """Before S6.8a the cancel check ran between corrections only (guide pulses aside)."""
        clock = FakeClock()
        mount = _AngularMount()
        policy = CollimationRecenterPolicy(
            mount, _calibration(), RecenterConfig(settle_ms=0), clock=clock, center_rate_x=8.0
        )
        checks: list[int] = []

        def cancel_check() -> bool:
            checks.append(1)
            return len(mount.moves) >= 1

        result = policy.center(
            lambda: _locked(140.0, 130.0), (100.0, 100.0), cancel_check=cancel_check
        )

        assert result.reason == "cancelled"
        assert len(mount.moves) == 1


class _HookLock:
    """A lock that runs a hook right after its next release -- places another thread's
    claim/release exactly between two of the executor's critical sections."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.on_release: Callable[[], None] | None = None

    def __enter__(self) -> _HookLock:
        self._lock.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._lock.release()
        hook, self.on_release = self.on_release, None
        if hook is not None:
            hook()


class _LatchMount(_AngularMount):
    def __init__(self) -> None:
        super().__init__()
        self.latched = False

    def abort(self) -> None:
        super().abort()
        self.latched = True

    def clear_abort(self) -> None:
        super().clear_abort()
        self.latched = False


class TestOwnerScopedAbortRace:
    def test_a_stale_cancel_never_latches_into_another_owners_new_motion(self) -> None:
        """Review (S6.8a): `abort(owner)` decided under the lock but sent the mount Stop after
        releasing it -- a reacquisition ending and Mount Align claiming in that gap got the
        stale Cancel's Stop latched into its fresh motion."""
        mount = _LatchMount()
        executor = MountMotionExecutor(mount)
        lock = _HookLock()
        executor._lock = lock  # type: ignore[assignment]
        reacquisition = executor.claim(REACQUISITION_OWNER, mount)
        assert not isinstance(reacquisition, str)
        claimed: list[MotionClaim | str] = []

        def reacquisition_ends_and_mount_align_starts() -> None:
            executor.release(reacquisition)
            claimed.append(executor.claim("Mount Align", mount))

        lock.on_release = reacquisition_ends_and_mount_align_starts
        executor.abort(REACQUISITION_OWNER)

        mount_align = claimed[0]
        assert not isinstance(mount_align, str)
        assert not mount_align.cancelled
        assert mount.latched is False, "the stale Cancel latched a Stop into Mount Align's move"


class TestTerrestrialTrackingRepair:
    """Review (S6.8a): Terrestrial + tracking ON refused every move with no way out. The #44
    owner repairs it (its own trail entry), then ONE fresh re-check; never in Astronomical."""

    def _setup(
        self, mode: OperatingMode, *, refuse_stop: bool = False
    ) -> tuple[MountMotionExecutor, TrackingEnforcer, FakeMountPark]:
        park = FakeMountPark(start_parked=False, refuse_stop_tracking=refuse_stop)
        park.make_available(tracking=True)
        enforcer = TrackingEnforcer(park, mode, settle_timeout_s=0, clock=FakeClock())
        return MountMotionExecutor(tracking_enforcer=enforcer), enforcer, park

    def test_terrestrial_tracking_is_repaired_by_the_enforcer_and_noted(self) -> None:
        executor, enforcer, park = self._setup(OperatingMode.TERRESTRIAL)

        decision = executor.axis_move_decision(
            True, "mount_align_move", reread=lambda: park.status().tracking
        )

        assert decision.allowed
        assert decision.note is not None and "turned tracking off" in decision.note
        assert park.status().tracking is False
        trail = enforcer.evidence()["transitions"]
        assert [t["context"] for t in trail] == ["mount_align_move", "mount_align_move:repair"]
        assert trail[-1]["command"] == "stop_tracking"

    def test_a_repair_that_does_not_take_says_how_to_recover(self) -> None:
        executor, _enforcer, park = self._setup(OperatingMode.TERRESTRIAL, refuse_stop=True)

        decision = executor.axis_move_decision(
            True, "mount_align_move", reread=lambda: park.status().tracking
        )

        assert not decision.allowed
        assert decision.reason == TERRESTRIAL_TRACKING_REPAIR_FAILED
        assert "To recover" in decision.reason
        assert park.stop_tracking_count == 1  # exactly one repair attempt

    def test_astronomical_tracking_is_never_repaired(self) -> None:
        executor, _enforcer, park = self._setup(OperatingMode.ASTRONOMICAL)

        decision = executor.axis_move_decision(
            True, "mount_align_move", reread=lambda: park.status().tracking
        )

        assert not decision.allowed
        assert decision.reason == ASTRONOMICAL_TRACKING_AXIS_MOVE_REFUSED
        assert park.stop_tracking_count == 0 and park.status().tracking is True

    def test_an_allowed_move_leaves_no_trail_entry(self) -> None:
        executor, enforcer, _park = self._setup(OperatingMode.TERRESTRIAL)
        before = list(enforcer.evidence()["transitions"])

        assert executor.axis_move_gate(False, "mount_align_move").allowed

        assert enforcer.evidence()["transitions"] == before


class TestInFlightListener:
    """S6.8c-R5: the global mount Stop follows `in_flight` through this notification."""

    def test_accepted_claims_and_their_releases_notify_refusals_and_stale_releases_do_not(
        self,
    ) -> None:
        executor = MountMotionExecutor()
        seen: list[str | None] = []

        def broken(_owner: str | None) -> None:
            raise RuntimeError("observer bug")

        executor.add_in_flight_listener(broken)
        executor.add_in_flight_listener(seen.append)
        claim = executor.claim("Mount Align")
        assert isinstance(claim, MotionClaim)
        assert isinstance(executor.claim("the Mount panel's park"), str)
        executor.release(claim)
        executor.release(claim)  # stale: already released

        assert seen == ["Mount Align", None]
        assert executor.in_flight is None
