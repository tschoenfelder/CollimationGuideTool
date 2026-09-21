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
this runner works *with* it rather than around it: it still reports the
switch command accepted at the INDI level even though nothing moved, so
a caller can't tell from the ack alone, which is why this runner ensures
unparked itself instead (reusing `IndiMountParkAdapter.unpark()`'s already-
durable TRACK_OFF) before ever pulsing.

Only calls the *full* `unpark()` when `status().parked` actually says so
right now; an already-unparked mount instead gets the lighter
`stop_tracking()`. A real live-hardware report ("You seem to enable
tracking... no wonder the frames look blury") traced back to unpark()
being called unconditionally on every submit() — including a run's later
steps, where the mount was already unparked by an earlier one —
resending the UNPARK switch command every time, which re-triggers
OnStep's own ~1.5s delayed auto-tracking-on quirk on every single pulse.
See `_run()`'s own comment for the full mechanism.

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
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass

from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.mount.port import AxisDirection, CommandResult, MountAxis, MountPort

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
#: mount offers `AngularMotionPort` (and a rate is installed); `duration_ms` is then the
#: equivalent timed duration, used only for the fallback below.
AngularPulseStep = tuple[MountAxis, AxisDirection, int, float]

#: OnStepAdapter's reasons for refusing an angular (center-mode, astronomy-grade) move that
#: its timed `manual` mode (tracking off, terrestrial) still allows: the mount is still at
#: home, or the clock is only plausible, not trusted. ONLY these fall back to the timed move
#: of the same size; every other refusal (limits, projected-target safety, parked, home
#: authority...) stays a refusal.
_AT_HOME_REFUSAL = "axis_motion_refused_at_home"
_TIMED_FALLBACK_REFUSALS = (_AT_HOME_REFUSAL, "raspberry_time_plausible_not_trusted")

MOTION_ANGULAR = "angular"
MOTION_TIMED = "timed"
MOTION_TIMED_FALLBACK = "timed_fallback"


@dataclass(frozen=True)
class MountPulseOutcome:
    """`pulsed` is False and `error` set if the mount never confirmed
    unparked or the pulse itself was rejected — see module docstring for
    why re-parking is always attempted regardless."""

    pulsed: bool
    error: str | None = None
    #: Issue #43 evidence (time.monotonic()): when the first pulse was commanded,
    #: when the last pulse's motion-off was confirmed, and when the settle finished.
    #: None when the sequence never got that far.
    motion_started_at: float | None = None
    motion_ended_at: float | None = None
    #: How each step actually moved (`MOTION_*`), in step order.
    motion_paths: tuple[str, ...] = ()
    settled_at: float | None = None


def _wait_for_parked(mount_park: MountParkPort, *, want_parked: bool, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if mount_park.status().parked == want_parked:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PARK_POLL_INTERVAL_S)


def _pulse_with_retry(
    mount: MountPort,
    axis: MountAxis,
    direction: AxisDirection,
    pulse_ms: int,
    rate_preset: str | None,
) -> CommandResult:
    """Retries a *rejected* pulse_axis() call a few times before giving
    up -- see `_PULSE_REJECTION_RETRIES`'s own docstring for why. An
    accepted result returns immediately on the first attempt; no retry
    overhead for the common case."""
    result = mount.pulse_axis(axis, direction, pulse_ms, rate_preset=rate_preset)
    attempt = 1
    while not result.accepted and attempt < _PULSE_REJECTION_RETRIES:
        time.sleep(_PULSE_REJECTION_RETRY_DELAY_S)
        result = mount.pulse_axis(axis, direction, pulse_ms, rate_preset=rate_preset)
        attempt += 1
    return result


_log = logging.getLogger(__name__)


def _move_with_retry(
    mount: MountPort,
    step: PulseStep | AngularPulseStep,
    rate_preset: str | None,
) -> tuple[CommandResult, str]:
    """One step: angular through OnStepAdapter when asked and supported, else timed. An
    angular refusal because the mount is still at home falls back to the timed move of
    the same equivalent duration (recorded in the path); any other refusal is final."""
    axis, direction, pulse_ms = step[0], step[1], step[2]
    arcsec = step[3] if len(step) > 3 else None
    move_angular = getattr(mount, "move_angular", None)
    if arcsec is not None and move_angular is not None:
        result = move_angular(axis, direction, arcsec)
        if result.accepted:
            return result, MOTION_ANGULAR
        if not any(token in result.message for token in _TIMED_FALLBACK_REFUSALS):
            return result, MOTION_ANGULAR
        _log.warning("angular move refused -- timed fallback: %s", result.message)
        return (
            _pulse_with_retry(mount, axis, direction, pulse_ms, rate_preset),
            MOTION_TIMED_FALLBACK,
        )
    return _pulse_with_retry(mount, axis, direction, pulse_ms, rate_preset), MOTION_TIMED


class MountTestMoveRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = False
        self._latest_outcome: MountPulseOutcome | None = None
        #: Issue #43: how many sequences were ever ACCEPTED -- lets a caller prove no
        #: pulse was commanded since it took a (verified, at-rest) reference frame.
        self.submit_count = 0

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
        that would undo any settle."""
        if not steps:
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self.submit_count += 1
        threading.Thread(
            target=self._run,
            args=(mount_park, mount, steps, rate_preset, park_after, settle_ms),
            daemon=True,
            name="mount-test-move",
        ).start()
        return True

    def _run(
        self,
        mount_park: MountParkPort,
        mount: MountPort,
        steps: Sequence[PulseStep | AngularPulseStep],
        rate_preset: str | None,
        park_after: bool,
        settle_ms: int,
    ) -> None:
        """Worker entry point. Issue #49: whatever happens inside the sequence -- a driver or
        network exception at ANY step -- an outcome is ALWAYS published and `_busy` ALWAYS
        cleared. A worker that died without doing so left the runner busy forever (buttons
        disabled, the Mount Align panel looking frozen)."""
        try:
            outcome = self._run_sequence(
                mount_park, mount, steps, rate_preset, park_after, settle_ms
            )
        except Exception as exc:  # noqa: BLE001 -- see the docstring: nothing may strand busy
            _log.exception("mount test-move worker crashed")
            outcome = MountPulseOutcome(pulsed=False, error=f"worker crashed: {exc!r}")
        with self._lock:
            self._latest_outcome = outcome
            self._busy = False

    def _run_sequence(
        self,
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
        # Real live-hardware report: "You seem to enable tracking. That
        # should not happen. No wonder, that the frames look blury" --
        # unpark() used to be called unconditionally on *every* submit(),
        # even across a run's later steps where the mount was already
        # confirmed unparked by an earlier one (Run Calibration's several
        # separate submit() calls, one per step -- unlike submit_sequence's
        # single call for several *sub*-pulses, which already only unparks
        # once). IndiMountParkAdapter.unpark() resends the UNPARK switch
        # command every time it runs, which re-triggers OnStep's own
        # ~1.5s delayed auto-tracking-on quirk (see that module's own
        # docstring) all over again on every single pulse -- landing the
        # driver's own tracking override right around when some step's
        # "after" frame gets captured. Only a mount that's actually
        # parked right now needs the full unpark() cycle; an
        # already-unparked one instead gets the lighter stop_tracking()
        # (a single TRACK_OFF, no UNPARK resend) -- still corrects any
        # tracking that crept back in, without re-arming the driver's own
        # UNPARK-linked quirk again.
        if mount_park.status().parked:
            mount_park.unpark()
        else:
            mount_park.stop_tracking()
            # Real diagnostic ba2b3259: `_wait_for_parked` just below is a
            # same-poll, zero-delay no-op on this branch -- status().parked
            # is already False (this mount was unparked by an earlier step
            # in the same "Run Calibration" run), so nothing gives the
            # driver's own separate motion-gate (see
            # `_PULSE_REJECTION_RETRIES`'s own docstring) any time to catch
            # up with the stop_tracking() just issued before the pulse loop
            # below fires its first attempt. In that real run 3 of 4 steps
            # hit exactly this and only succeeded on retry, each paying the
            # round trip through a doomed first attempt before
            # `_PULSE_REJECTION_RETRY_DELAY_S` even started counting.
            # Reusing that same already-proven-sufficient delay here, up
            # front, avoids that round trip in the common case; retry
            # (below) still covers the rest.
            time.sleep(_PULSE_REJECTION_RETRY_DELAY_S)
        try:
            if not _wait_for_parked(mount_park, want_parked=False, timeout_s=_UNPARK_TIMEOUT_S):
                error = "mount did not confirm unparked in time -- aborting test move"
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
                motion_started_at = time.monotonic()
                for step in steps:
                    axis, direction = step[0], step[1]
                    result, path = _move_with_retry(mount, step, rate_preset)
                    paths.append(path)
                    if not result.accepted:
                        error = (
                            f"pulse rejected for {axis.name} {direction.name} "
                            f"({path}): {result.message}"
                        )
                        break
                else:
                    pulsed = True
                    motion_ended_at = time.monotonic()
                    if settle_ms > 0:
                        time.sleep(settle_ms / 1000.0)
                    settled_at = time.monotonic()
        finally:
            if park_after:
                # Always try to leave the mount parked again, even if the
                # pulse failed above -- see module docstring.
                mount_park.park()
                if not _wait_for_parked(mount_park, want_parked=True, timeout_s=_REPARK_TIMEOUT_S):
                    error = error or "mount did not confirm re-parked in time"

        return MountPulseOutcome(
            pulsed=pulsed,
            error=error,
            motion_started_at=motion_started_at,
            motion_ended_at=motion_ended_at,
            settled_at=settled_at,
            motion_paths=tuple(paths),
        )

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
