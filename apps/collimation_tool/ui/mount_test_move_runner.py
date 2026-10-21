"""MountTestMoveRunner — runs one mount unpark/pulse/re-park sequence on
a background thread, off the UI thread.

Mirrors `FovCalibrator`'s shape (submit()/take_latest()/is_busy, "run at
most one at a time", daemon background thread) for the same reason: the
pulse itself blocks for the requested duration
(`IndiMountPulseAdapter.pulse_axis` deliberately sleeps out the full
pulse — see that module's docstring), so doing this inline from a button
click would freeze the window for the whole test.

Deliberately does *not* touch any camera/frame accessor, unlike an
earlier version of this module — a real crash was traced to exactly
that: calling `CameraPanel.latest_mono_frame()` from this background
thread while the same panel's own Qt poll timer was concurrently
delivering new frames on the main thread (both touch the panel's
captured-frame state; `FovCalibrator`'s own established pattern never
has this problem because its background thread only ever processes
plain numpy arrays already captured on the main thread before submit()).
So here: `MountTestMovePanel` captures the "before" frames itself on the
main thread, submits only the mount sequence to this runner, and — once
`take_latest()` reports the pulse finished — captures the "after" frames
and computes the response itself, also on the main thread (fast enough,
unlike FOV registration's search, not to need its own thread). This
runner's only job is the unpark/pulse/re-park timing.

Always ensures unparked first — a real-hardware check found OnStep refuses
motion commands while parked ("Please unpark the mount before
issuing any motion/sync commands"). That refusal is a deliberate safety
interlock, not a defect — parked is supposed to mean "don't move" — so
this runner works *with* it rather than around it: it unparks
(`MountParkPort.unpark()`, confirmed "unparked with tracking off") only when a FRESH reading
says the mount is parked right now -- a real live-hardware report ("You seem to enable
tracking... no wonder the frames look blury") traced back to unpark() being resent on every
submit(), re-triggering OnStep's ~1.5 s delayed auto-tracking-on quirk -- and then runs #44's
unpark enforcement through the app-wide executor (S6.8a, F12), exactly as the Mount panel does
after its own unpark.

S6.8a (movement-paths F1, M03 removed): the runner no longer sends `stop_tracking()` (OnStep's
EMERGENCY stop over INDI) before every move. Whether an axis move may run on a tracking mount is
the executor's tracking rule (`MountMotionExecutor.axis_move_gate`, owned by the
`TrackingEnforcer`): a tracking mount is refused with the mode's reason and tracking is left as
it is -- Terrestrial relies on the enforcer keeping tracking OFF; in Astronomical mode Mount
Align's axis moves are refused while tracking (guide-pulse Mount Align is S6.8b). Every sequence
runs under one `MotionClaim` of that executor: one in-flight mount motion app-wide (a refused
claim is published at once as the outcome, with the busy reason, never queued), and the
executor's `abort()` (Stop, quit) cancels this runner's waits too.

Whether it *re-parks* afterward is the caller's choice (`park_after`,
default `True`). The mount-alignment feature (`MountTestMovePanel`) always
passes `park_after=False`: its "Run Calibration" sequence and its per-camera
direction-pad nudges are meant to run one after another across a single
unparked working session — re-parking after every individual pulse would
undo the whole point of leaving the mount unparked between them. Parking
back up when the session is done stays the separate Mount panel's job,
exactly as it already is for every other unparked action in this app.
`park_after=True` (e.g. a hypothetical single ad hoc probe) still re-parks
in a `finally`, even if the pulse itself failed, so a mid-run error can't
strand the mount unparked in that mode.

Time (issue #53, S3b): every wait and timestamp here -- the fresh park reading,
the unpark/re-park confirmation polls, rejection
retries, the settle and the `MountPulseOutcome` stamps -- goes through the
`Clock` given to the runner (`clock=`, default the real `SYSTEM_CLOCK`), so tests
drive them on a `FakeClock`.

Stop (`abort()`, S6.3): every one of those waits takes the Stop event, so a Stop ends the
wait in progress at once -- not only the next step -- and nothing further is sent to the
mount: no unpark after the fresh park reading, no further pulse or rejection
retry, no re-park command (a re-park already commanded is no longer awaited). The settle is
cut short like a Stop during the last move (the move itself counts as done).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from astrotool_core.mount.park_port import MountParkPort, MountParkStatus
from astrotool_core.mount.port import (
    AngularMotionPort,
    AxisDirection,
    CommandResult,
    MountAxis,
    MountPort,
)
from astrotool_core.timing import SYSTEM_CLOCK, Clock, Deadline

from collimation_tool.application.mount_align_guide import (
    MOTION_GUIDE_PULSE,
    guide_pulse_refusal,
    send_guide_pulse_move,
)
from collimation_tool.application.mount_motion import (
    MotionClaim,
    MountAlignPrimitive,
    MountMotionExecutor,
)

#: The claim owner name shown in another workflow's busy refusal.
MOUNT_ALIGN_OWNER = "Mount Align"

_UNPARK_TIMEOUT_S = 5.0
_REPARK_TIMEOUT_S = 5.0
_PARK_POLL_INTERVAL_S = 0.1
#: Real report 45e5ae86 ("SEV 1"): "Shows unparked but fails for parked."
#: Confirmed live against the real rig: status().parked can settle to
#: False while a pulse moments later still gets rejected -- the driver's
#: own "unparked" announcement and its internal motion-gate (what
#: IndiMountPulseAdapter.pulse_axis() actually gets rejected against, see
#: that module's own docstring) aren't perfectly synced. _wait_for_parked
#: above already waits out the *announcement*; this retries through the
#: *gate* separately, since they're evidently not the same thing landing
#: at the same time.
_PULSE_REJECTION_RETRIES = 6
_PULSE_REJECTION_RETRY_DELAY_S = 0.3

#: One pulse within a submitted sequence: (axis, direction, duration_ms).
PulseStep = tuple[MountAxis, AxisDirection, int]
#: The same, but executed as an ANGULAR move of `arcsec` through OnStepAdapter when the
#: mount offers `AngularMotionPort`; `duration_ms` is then the equivalent timed duration, used
#: only for the fallback below (and never on a mount that reports no timed pulses, S6.0).
AngularPulseStep = tuple[MountAxis, AxisDirection, int, float]

#: OnStepAdapter's reasons for refusing an angular (center-mode, astronomy-grade) move that
#: its timed `manual` mode (tracking off, terrestrial) still allows: the mount is still at
#: home, or the clock is only plausible, not trusted. ONLY these fall back to the timed move
#: of the same size; every other refusal (limits, projected-target safety, parked, home
#: authority...) stays a refusal.
_AT_HOME_REFUSAL = "axis_motion_refused_at_home"
_TIMED_FALLBACK_REFUSALS = (_AT_HOME_REFUSAL, "raspberry_time_plausible_not_trusted")

MOTION_ANGULAR = "angular"
#: Same wording as the OnStep adapter's own Stop refusal, so either reads the same.
_STOPPED_BY_USER = "stopped by the user"
MOTION_TIMED = "timed"
MOTION_TIMED_FALLBACK = "timed_fallback"


@dataclass(frozen=True)
class MountPulseOutcome:
    """`pulsed` is False and `error` set if the mount never confirmed
    unparked or the pulse itself was rejected — see module docstring for
    why re-parking is always attempted regardless."""

    pulsed: bool
    error: str | None = None
    #: Issue #43 evidence (on the runner's clock -- the real `time.monotonic()` in
    #: production): when the first pulse was commanded,
    #: when the last pulse's motion-off was confirmed, and when the settle finished.
    #: None when the sequence never got that far.
    motion_started_at: float | None = None
    motion_ended_at: float | None = None
    #: How each step actually moved (`MOTION_*`), in step order.
    motion_paths: tuple[str, ...] = ()
    settled_at: float | None = None
    #: S6.8a review: something the operator must be told even though the move ran (e.g. the
    #: Terrestrial tracking policy had to turn tracking off first). None = nothing.
    note: str | None = None
    #: S6.8b review #2: a guide-pulse step that failed or was stopped partway -- the guide-ms
    #: of its pulses that COMPLETED (the mount did move them), and of a pulse that was sent
    #: and then failed (it may have moved by any part of it). 0 otherwise.
    guide_ms_sent: int = 0
    guide_ms_uncertain: int = 0


#: `_wait_for_parked`'s answer when Stop ended the wait.
_WAIT_STOPPED = "stopped"


def _wait_for_parked(
    mount_park: MountParkPort,
    *,
    want_parked: bool,
    timeout_s: float,
    clock: Clock = SYSTEM_CLOCK,
    cancel: threading.Event | None = None,
) -> str | None:
    """None once a FRESH reading confirms `want_parked` (S6.0c: never a held-over one), else
    why not: the mount never confirmed, its connection stayed busy (no fresh reading), or
    `_WAIT_STOPPED` -- Stop (`cancel`) ended the wait (S6.3).

    Order per round: read -> deadline check -> wait one poll interval, so a reading taken
    exactly at the deadline still counts (`Deadline` convention: expired once now >= it)."""
    deadline = Deadline.after(timeout_s, clock=clock)
    while True:
        status = mount_park.status()
        if status.fresh and status.parked == want_parked:
            return None
        if deadline.expired():
            if not status.fresh:
                return "the mount connection stayed busy (no fresh reading)"
            return "not confirmed"
        if not clock.sleep(_PARK_POLL_INTERVAL_S, cancel):
            return _WAIT_STOPPED


#: S6.0c: the unpark-or-not decision needs a FRESH park reading; a held-over one (another
#: operation holds the mount connection) may be wrong.
_PARK_STATE_UNKNOWN = (
    "mount busy -- its park state could not be read freshly in time; nothing was sent"
)


def _fresh_park_status(
    mount_park: MountParkPort,
    *,
    timeout_s: float,
    clock: Clock = SYSTEM_CLOCK,
    cancel: threading.Event | None = None,
) -> MountParkStatus | None:
    """A fresh park reading (S6.0c), waiting -- on this worker thread, bounded -- while another
    operation holds the connection; None if none arrives in time or Stop (`cancel`) ended the
    wait (the caller tells the two apart by the event). Same round order as
    `_wait_for_parked`."""
    deadline = Deadline.after(timeout_s, clock=clock)
    while True:
        status = mount_park.status()
        if status.fresh:
            return status
        if deadline.expired():
            return None
        if not clock.sleep(_PARK_POLL_INTERVAL_S, cancel):
            return None


def _pulse_with_retry(
    mount: MountPort,
    axis: MountAxis,
    direction: AxisDirection,
    pulse_ms: int,
    rate_preset: str | None,
    clock: Clock = SYSTEM_CLOCK,
    cancel: threading.Event | None = None,
) -> CommandResult:
    """Retries a *rejected* pulse_axis() call a few times before giving
    up -- see `_PULSE_REJECTION_RETRIES`'s own docstring for why. An
    accepted result returns immediately on the first attempt; no retry
    overhead for the common case. S6.3: Stop (`cancel`) ends the retry delay
    at once and no further attempt is sent (S3b finding (a))."""
    result = mount.pulse_axis(axis, direction, pulse_ms, rate_preset=rate_preset)
    attempt = 1
    while not result.accepted and attempt < _PULSE_REJECTION_RETRIES:
        if not clock.sleep(_PULSE_REJECTION_RETRY_DELAY_S, cancel):
            return CommandResult(
                accepted=False,
                message=f"{_STOPPED_BY_USER} -- no retry sent (last: {result.message})",
            )
        result = mount.pulse_axis(axis, direction, pulse_ms, rate_preset=rate_preset)
        attempt += 1
    return result


_log = logging.getLogger(__name__)


def supports_timed_pulse(mount: MountPort) -> bool:
    """Whether `mount.pulse_axis` (a timed move) can move this mount at all.

    S6.0 (#46/#31): the single authoritative source is the adapter's own reported capability,
    `capabilities().supports_pulse_guiding` -- `OnStepMountPulseAdapter` (OnStepAdapter >= 0.4
    over INDI) reports False because no timed primitive exists there; its only motion is the
    angular `move_angular`. Callers never infer this from a refusal message."""
    return bool(mount.capabilities().supports_pulse_guiding)


_NO_TIMED_PULSE = (
    "this mount reports no timed pulse support (capabilities().supports_pulse_guiding is "
    "False) and the move carried no angular size -- nothing was sent to the mount"
)


def _move_with_retry(
    mount: MountPort,
    step: PulseStep | AngularPulseStep,
    rate_preset: str | None,
    clock: Clock = SYSTEM_CLOCK,
    cancel: threading.Event | None = None,
) -> tuple[CommandResult, str]:
    """One step: angular through OnStepAdapter when asked and supported, else timed. An
    angular refusal because the mount is still at home falls back to the timed move of
    the same equivalent duration (recorded in the path) -- ONLY on a mount that reports timed
    pulses; any other refusal, and every refusal on a mount without timed pulses, is final and
    reported as the adapter gave it (S6.0, audit P02: a fallback onto a path known to be
    unsupported only replaced the real refusal reason)."""
    axis, direction, pulse_ms = step[0], step[1], step[2]
    arcsec = step[3] if len(step) > 3 else None
    capabilities = mount.capabilities()
    timed_supported = bool(capabilities.supports_pulse_guiding)
    # S6.8a (#55 C02): angular moves as the adapter REPORTS them, not a getattr probe.
    if arcsec is not None and capabilities.supports_angular_moves:
        result = cast(AngularMotionPort, mount).move_angular(axis, direction, arcsec)
        if result.accepted:
            return result, MOTION_ANGULAR
        if not timed_supported or not any(
            token in result.message for token in _TIMED_FALLBACK_REFUSALS
        ):
            return result, MOTION_ANGULAR
        _log.warning("angular move refused -- timed fallback: %s", result.message)
        return (
            _pulse_with_retry(mount, axis, direction, pulse_ms, rate_preset, clock, cancel),
            MOTION_TIMED_FALLBACK,
        )
    if not timed_supported:
        return CommandResult(accepted=False, message=_NO_TIMED_PULSE), MOTION_TIMED
    return (
        _pulse_with_retry(mount, axis, direction, pulse_ms, rate_preset, clock, cancel),
        MOTION_TIMED,
    )


class MountTestMoveRunner:
    def __init__(
        self, *, clock: Clock | None = None, executor: MountMotionExecutor | None = None
    ) -> None:
        """`clock` (#53): every wait and outcome timestamp of the worker; default the real
        clock. Tests pass a `FakeClock` -- auto-advance runs a sequence's waits instantly,
        manual mode lets a test watch the worker inside a wait (`wait_for_sleepers`).

        `executor` (S6.8a): the app-wide mount-motion executor every sequence claims; None = a
        private one without a mode policy until the owner hands over its own (`use_executor`)."""
        self._clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        self._executor = executor if executor is not None else MountMotionExecutor()
        self._lock = threading.Lock()
        self._busy = False
        self._latest_outcome: MountPulseOutcome | None = None
        #: Issue #43: how many sequences were ever ACCEPTED -- lets a caller prove no
        #: pulse was commanded since it took a (verified, at-rest) reference frame.
        self.submit_count = 0
        #: S6.0c/S6.8a: the running sequence's claim; Stop sets its cancel event, which ends
        #: the whole submitted sequence -- no further step is sent.
        self._claim: MotionClaim | None = None
        #: The running sequence's operator note (worker thread only).
        self._note: str | None = None

    @property
    def executor(self) -> MountMotionExecutor:
        return self._executor

    def use_executor(self, executor: MountMotionExecutor) -> None:
        """S6.8a: claim every later sequence from `executor` (the panel hands over the one it
        was wired with). Never while a sequence runs."""
        with self._lock:
            if self._busy:
                raise RuntimeError("cannot change the motion executor while a move runs")
            self._executor = executor

    def submit(
        self,
        mount_park: MountParkPort,
        mount: MountPort,
        axis: MountAxis,
        direction: AxisDirection,
        pulse_ms: int,
        *,
        rate_preset: str | None = None,
        park_after: bool = True,
        settle_ms: int = 0,
    ) -> bool:
        """Start an unpark/pulse/(re-park unless `park_after=False`)
        sequence in the background. Returns False (a no-op) if one is
        already running. A thin one-step wrapper around `submit_sequence` —
        see that method and the module docstring for `park_after`/`settle_ms`."""
        return self.submit_sequence(
            mount_park,
            mount,
            [(axis, direction, pulse_ms)],
            rate_preset=rate_preset,
            park_after=park_after,
            settle_ms=settle_ms,
        )

    def submit_sequence(
        self,
        mount_park: MountParkPort,
        mount: MountPort,
        steps: Sequence[PulseStep | AngularPulseStep],
        *,
        rate_preset: str | None = None,
        park_after: bool = True,
        settle_ms: int = 0,
        primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
    ) -> bool:
        """Like `submit`, but for several pulses run back-to-back after a
        single unpark (e.g. a composed two-axis direction-pad nudge) —
        `MountTestMovePanel` needs one clean before/after measurement
        bracketing the *whole* sequence, not one per sub-pulse. Returns
        False (a no-op) if one is already running, or if `steps` is empty.

        `settle_ms`: real report ("calibration doesn't wait for mount to
        be stabilized") -- the caller's "after" capture used to happen
        the instant the last pulse's motion-off was confirmed, with no
        allowance for mechanical settle (backlash/vibration damping out)
        between the motor physically stopping and the mount actually
        being at rest. When every step in `steps` succeeds, this blocks
        (still on the background thread -- `is_busy` stays True) for
        `settle_ms` *before* reporting done, so a caller waiting on
        `is_busy` to capture its "after" frame naturally waits it out
        too. Applied once after the whole sequence, not per sub-pulse.
        Skipped entirely if the sequence was rejected (no valid "after"
        state to settle into) or if `park_after=True` -- runs before
        park() either way, since re-parking is itself further motion
        that would undo any settle.

        S6.8a: the sequence claims the app-wide mount-motion executor. While another workflow
        moves the mount the claim is refused: nothing is sent, and the refusal (its busy
        reason) is published at once as this request's outcome (`pulsed=False`), so the
        caller reports it like any other failed move -- never queued, never silently dropped.
        A refused claim is not counted in `submit_count`.

        S6.8b: `primitive` GUIDE_PULSE sends every step's `duration_ms` as OnStepAdapter >= 0.5.0
        guide pulses while the mount keeps tracking (`_run_guide_sequence`); AXIS (default) is
        the axis-move path above, unchanged."""
        if not steps:
            return False
        with self._lock:
            if self._busy:
                return False
            # S6.0c: a new operator command re-arms after an earlier Stop -- the executor does
            # that only for an accepted claim, so a refused submit never re-arms the sequence
            # that Stop interrupted.
            claim = self._executor.claim(MOUNT_ALIGN_OWNER, mount)
            if isinstance(claim, str):
                self._latest_outcome = MountPulseOutcome(pulsed=False, error=claim)
                return True
            self._busy = True
            self.submit_count += 1
            self._claim = claim
        threading.Thread(
            target=self._run,
            args=(claim, mount_park, mount, steps, rate_preset, park_after, settle_ms, primitive),
            daemon=True,
            name="mount-test-move",
        ).start()
        return True

    def _run(
        self,
        claim: MotionClaim,
        mount_park: MountParkPort,
        mount: MountPort,
        steps: Sequence[PulseStep | AngularPulseStep],
        rate_preset: str | None,
        park_after: bool,
        settle_ms: int,
        primitive: MountAlignPrimitive = MountAlignPrimitive.AXIS,
    ) -> None:
        """Worker entry point. Issue #49: whatever happens inside the sequence -- a driver or
        network exception at ANY step -- an outcome is ALWAYS published, `_busy` ALWAYS
        cleared and (S6.8a) the executor claim ALWAYS released. A worker that died without
        doing so left the runner busy forever (buttons disabled, the Mount Align panel looking
        frozen)."""
        try:
            if primitive is MountAlignPrimitive.GUIDE_PULSE:
                outcome = self._run_guide_sequence(claim, mount_park, mount, steps, settle_ms)
            else:
                outcome = self._run_sequence(
                    claim, mount_park, mount, steps, rate_preset, park_after, settle_ms
                )
        except Exception as exc:  # noqa: BLE001 -- see the docstring: nothing may strand busy
            _log.exception("mount test-move worker crashed")
            outcome = MountPulseOutcome(pulsed=False, error=f"worker crashed: {exc!r}")
        finally:
            self._executor.release(claim)
        with self._lock:
            self._latest_outcome = outcome
            self._busy = False
            self._claim = None

    def _axis_move_refusal(
        self,
        claim: MotionClaim,
        mount_park: MountParkPort,
        steps: Sequence[PulseStep | AngularPulseStep],
        initial: MountParkStatus,
    ) -> str | None:
        """S6.8a, after the mount is confirmed unparked: F12 -- an automatic unpark gets #44's
        unpark enforcement, like the Mount panel's own; R1 -- an axis move on a tracking mount
        is refused (tracking is never stopped here). None = the sequence may move."""
        stop = claim.cancel
        tracking: bool | None = initial.tracking
        if initial.parked:
            gate = self._executor.enforce_after_unpark(stop)
            if stop.is_set():
                return f"{_STOPPED_BY_USER} -- no move sent"
            if gate is not None and not gate.allowed:
                return f"after the automatic unpark: {gate.reason} -- no move sent"
            status = _fresh_park_status(
                mount_park, timeout_s=_UNPARK_TIMEOUT_S, clock=self._clock, cancel=stop
            )
            if stop.is_set():
                return f"{_STOPPED_BY_USER} -- no move sent"
            tracking = None if status is None else status.tracking
        if not any(len(step) > 3 for step in steps):
            return None  # timed pulses only: no axis GOTO, tracking is no precondition

        def reread() -> bool | None:
            fresh = _fresh_park_status(
                mount_park, timeout_s=_UNPARK_TIMEOUT_S, clock=self._clock, cancel=stop
            )
            return None if fresh is None else fresh.tracking

        decision = self._executor.axis_move_decision(
            tracking, "mount_align_move", reread=reread, cancel=stop
        )
        if decision.note is not None:
            _log.warning("Mount Align: %s", decision.note)
            self._note = decision.note
        return None if decision.allowed else decision.reason

    def _run_guide_sequence(
        self,
        claim: MotionClaim,
        mount_park: MountParkPort,
        mount: MountPort,
        steps: Sequence[PulseStep | AngularPulseStep],
        settle_ms: int,
    ) -> MountPulseOutcome:
        """S6.8b: the steps as guide pulses while tracking. Nothing is unparked, parked or
        tracking-changed here: a FRESH reading must show the mount unparked and tracking, the
        mode still Astronomical and the capability reported (`guide_pulse_refusal`), else
        nothing is sent. Each step is split into the adapter's pulse range with Stop checked
        between pulses (`send_guide_pulse_move`); the settle is cut short by Stop."""
        clock = self._clock
        stop = claim.cancel
        status = _fresh_park_status(
            mount_park, timeout_s=_UNPARK_TIMEOUT_S, clock=clock, cancel=stop
        )
        if stop.is_set():
            return MountPulseOutcome(
                pulsed=False, error=f"{_STOPPED_BY_USER} -- nothing was sent to the mount"
            )
        if status is None:
            return MountPulseOutcome(pulsed=False, error=_PARK_STATE_UNKNOWN)
        enforcer = self._executor.tracking_enforcer
        refusal = guide_pulse_refusal(
            enforcer.mode if enforcer is not None else None,
            parked=status.parked,
            tracking=status.tracking if status.available else None,
            mount=mount,
        )
        if refusal is not None:
            return MountPulseOutcome(pulsed=False, error=refusal)
        paths: list[str] = []
        motion_started_at = clock.monotonic()
        for step in steps:
            axis, direction, duration_ms = step[0], step[1], step[2]
            if stop.is_set():
                return MountPulseOutcome(
                    pulsed=False,
                    error=f"{_STOPPED_BY_USER} -- {axis.name} {direction.name} not sent",
                    motion_started_at=motion_started_at,
                    motion_paths=tuple(paths),
                )
            result, sent_ms, uncertain_ms = send_guide_pulse_move(
                mount, axis, direction, duration_ms, stop
            )
            paths.append(MOTION_GUIDE_PULSE)
            if not result.accepted:
                return MountPulseOutcome(
                    pulsed=False,
                    error=(
                        f"pulse rejected for {axis.name} {direction.name} "
                        f"({MOTION_GUIDE_PULSE}): {result.message}"
                    ),
                    motion_started_at=motion_started_at,
                    motion_paths=tuple(paths),
                    guide_ms_sent=sent_ms,
                    guide_ms_uncertain=uncertain_ms,
                )
        motion_ended_at = clock.monotonic()
        if settle_ms > 0 and not stop.is_set():
            clock.sleep(settle_ms / 1000.0, stop)  # S6.3: Stop cuts it short
        return MountPulseOutcome(
            pulsed=True,
            motion_started_at=motion_started_at,
            motion_ended_at=motion_ended_at,
            settled_at=clock.monotonic(),
            motion_paths=tuple(paths),
        )

    def _run_sequence(
        self,
        claim: MotionClaim,
        mount_park: MountParkPort,
        mount: MountPort,
        steps: Sequence[PulseStep | AngularPulseStep],
        rate_preset: str | None,
        park_after: bool,
        settle_ms: int,
    ) -> MountPulseOutcome:
        pulsed = False
        paths: list[str] = []
        error: str | None = None
        motion_started_at: float | None = None
        motion_ended_at: float | None = None
        settled_at: float | None = None
        self._note = None
        # Only a mount that is parked right now (a FRESH reading) gets unpark(): resending
        # UNPARK on every submit re-triggered OnStep's delayed auto-tracking-on quirk (module
        # docstring). S6.8a: an unparked mount gets NO command here -- the former
        # stop_tracking() was OnStep's emergency stop before every move (M03/F1).
        clock = self._clock
        stop = claim.cancel
        initial = _fresh_park_status(
            mount_park, timeout_s=_UNPARK_TIMEOUT_S, clock=clock, cancel=stop
        )
        if stop.is_set():  # S6.3: Stop during (or right after) the fresh-park wait
            return MountPulseOutcome(
                pulsed=False, error=f"{_STOPPED_BY_USER} -- nothing was sent to the mount"
            )
        if initial is None:
            return MountPulseOutcome(pulsed=False, error=_PARK_STATE_UNKNOWN)
        if initial.parked:
            mount_park.unpark()
        try:
            why = _wait_for_parked(
                mount_park, want_parked=False, timeout_s=_UNPARK_TIMEOUT_S, clock=clock,
                cancel=stop,
            )
            refusal = (
                None
                if why is not None
                else self._axis_move_refusal(claim, mount_park, steps, initial)
            )
            if why == _WAIT_STOPPED:
                error = f"{_STOPPED_BY_USER} -- unpark not confirmed, no move sent"
            elif why is not None:
                error = f"mount did not confirm unparked in time ({why}) -- aborting test move"
            elif refusal is not None:
                error = refusal
            else:
                # All-or-nothing `pulsed` flag, same as the single-pulse
                # contract this generalizes: if a later step in a multi-step
                # sequence is rejected, an earlier step may already have
                # physically moved the mount, but `pulsed=False` is still
                # reported rather than a partial-success shape -- a
                # mid-sequence rejection is an edge case that in practice
                # only happens if the mount disconnects mid-run, and the
                # caller treating "not fully pulsed" as "don't trust an
                # after-measurement" is the safer default.
                motion_started_at = clock.monotonic()
                for step in steps:
                    axis, direction = step[0], step[1]
                    if stop.is_set():
                        error = f"{_STOPPED_BY_USER} -- {axis.name} {direction.name} not sent"
                        break
                    result, path = _move_with_retry(mount, step, rate_preset, clock, stop)
                    paths.append(path)
                    if not result.accepted:
                        error = (
                            f"pulse rejected for {axis.name} {direction.name} "
                            f"({path}): {result.message}"
                        )
                        break
                else:
                    pulsed = True
                    motion_ended_at = clock.monotonic()
                    if settle_ms > 0 and not stop.is_set():
                        clock.sleep(settle_ms / 1000.0, stop)  # S6.3: Stop cuts it short
                    settled_at = clock.monotonic()
        finally:
            if park_after:
                # Always try to leave the mount parked again, even if the
                # pulse failed above -- see module docstring. S6.3: but never
                # after Stop -- a park is motion, and Stop means no further motion.
                if stop.is_set():
                    error = error or f"{_STOPPED_BY_USER} -- re-park not sent"
                else:
                    mount_park.park()
                    why = _wait_for_parked(
                        mount_park, want_parked=True, timeout_s=_REPARK_TIMEOUT_S, clock=clock,
                        cancel=stop,
                    )
                    if why == _WAIT_STOPPED:
                        error = error or f"{_STOPPED_BY_USER} -- re-park not confirmed"
                    elif why is not None:
                        error = error or f"mount did not confirm re-parked in time ({why})"

        return MountPulseOutcome(
            pulsed=pulsed,
            error=error,
            motion_started_at=motion_started_at,
            motion_ended_at=motion_ended_at,
            settled_at=settled_at,
            motion_paths=tuple(paths),
            note=self._note,
        )

    def abort(self) -> None:
        """Stop (S6.0c, any thread, returns at once): no further step of the running sequence
        is sent, and (S6.3) a runner wait in progress ends at once. Stopping the mount itself
        is the mount adapter's `abort()` (the executor's `abort()` sends both); a step already
        inside the adapter ends there, bounded by the adapter."""
        with self._lock:
            claim = self._claim
        if claim is not None:
            claim.cancel.set()

    def take_latest(self) -> MountPulseOutcome | None:
        """Return and clear the latest completed outcome, if any — None
        means no test move has finished since the last call."""
        with self._lock:
            outcome, self._latest_outcome = self._latest_outcome, None
            return outcome

    @property
    def is_busy(self) -> bool:
        with self._lock:
            return self._busy
