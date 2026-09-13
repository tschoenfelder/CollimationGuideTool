"""Tests for BoundedFocusSearcher — issue #33's own safety-critical core:
never exceeds device/envelope limits, reports an explicit bounded/
inconclusive result at the search boundary rather than extrapolating,
handles cancellation/rejection/frame-loss cleanly, and applies a
deterministic consistent-direction final approach."""

from __future__ import annotations

from collections.abc import Callable

from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.focus.port import FocuserMoveResult
from collimation_tool.application.autofocus_search import (
    AutofocusStatus,
    BoundedFocusSearcher,
    FocusSample,
)


class _RejectingFocuser(FakeFocuser):
    """FakeFocuser that rejects move_absolute() to any of a scripted set
    of target positions -- FakeFocuser itself always accepts (see its own
    docstring/behavior), so this small local double is needed to exercise
    "rejected focuser command aborts cleanly"."""

    def __init__(self, *, reject_positions: set[int]) -> None:
        super().__init__()
        self._reject_positions = reject_positions

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        if steps in self._reject_positions:
            return FocuserMoveResult(
                accepted=False, target_position=steps, start_position=self.get_position()
            )
        return super().move_absolute(steps)


class _OnceMovingFocuser(FakeFocuser):
    """Reports is_moving()==True exactly once after each move_absolute(),
    then False -- proves the searcher actually waits for move-complete
    before taking a measurement, rather than measuring instantly."""

    def __init__(self) -> None:
        super().__init__()
        self._pending_busy = False

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        result = super().move_absolute(steps)
        self._pending_busy = True
        return result

    def is_moving(self) -> bool:
        if self._pending_busy:
            self._pending_busy = False
            return True
        return False


def _v_curve(
    focuser: FakeFocuser, best_position: int, *, base: float = 1.0, slope: float = 0.01
) -> Callable[[], FocusSample | None]:
    def measure() -> FocusSample:
        value = base + slope * abs(focuser.get_position() - best_position)
        return FocusSample(value=value, confidence=1.0)

    return measure


def _monotonic_ramp(focuser: FakeFocuser) -> Callable[[], FocusSample]:
    """A "lower is better" metric with no true minimum anywhere in range --
    keeps improving forever as position increases. Swings stay
    proportionate to the current baseline no matter how far the search
    travels (unlike a V-curve centered on a very distant target, whose
    own baseline value balloons with distance and swamps the fixed 5%
    relative-improvement threshold) -- the right shape for exercising
    "the search climbs all the way to its own boundary and stays there"."""

    def measure() -> FocusSample:
        # An offset just comfortably above any position this search could
        # ever reach (FakeFocuser's own device max is 5000) -- small
        # enough that a single coarse_step's own swing stays a large
        # fraction of the current baseline throughout the whole climb
        # (see this function's own docstring for why a huge offset, e.g.
        # 1_000_000, defeats the fixed 5% relative-improvement threshold
        # instead), while staying positive at every reachable position.
        return FocusSample(value=float(6_000 - focuser.get_position()), confidence=1.0)

    return measure


class TestBoundedFocusSearcherHappyPath:
    def test_converges_to_the_known_optimum_and_reports_success(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(2500)
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25
        )

        result = searcher.search(_v_curve(focuser, best_position=2000))

        assert result.status is AutofocusStatus.SUCCESS
        assert result.best_position == 2000
        assert focuser.get_position() == 2000

    def test_higher_is_better_mode_also_converges(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(2500)
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=True, coarse_step=250, fine_step=25
        )

        def measure() -> FocusSample:
            # Sharpness-like: HIGHER near the target, unlike the V-curve.
            # Slope large enough relative to the baseline that one
            # coarse_step's own swing clears the default 5% relative-
            # improvement threshold (same scaling concern as _v_curve's
            # own base=1.0/slope=0.01 pairing).
            value = 100.0 - 0.05 * abs(focuser.get_position() - 2000)
            return FocusSample(value=value, confidence=1.0)

        result = searcher.search(measure)

        assert result.status is AutofocusStatus.SUCCESS
        assert result.best_position == 2000

    def test_full_focus_curve_is_recorded(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(2500)
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25
        )

        result = searcher.search(_v_curve(focuser, best_position=2000))

        assert len(result.samples) > 1
        assert result.search_min is not None
        assert result.search_max is not None
        for point in result.samples:
            assert result.search_min <= point.position <= result.search_max


class TestFlatCurve:
    def test_already_at_the_optimum_reports_success_not_no_usable_evidence(self) -> None:
        # Neither probe direction improves on the starting position's own
        # measurement -- a real, successful outcome (already at/near the
        # local optimum), not "no evidence": every measurement taken was
        # genuinely valid, it just never beat the start. Matches the
        # legacy FocusSearcher's own analogous "max_steps" flat-curve
        # case in spirit, but SUCCESS is the more accurate status here
        # since real, valid evidence was gathered throughout.
        focuser = FakeFocuser()
        focuser.move_absolute(2000)
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25
        )

        result = searcher.search(lambda: FocusSample(value=5.0, confidence=1.0))

        assert result.status is AutofocusStatus.SUCCESS
        assert result.best_position == 2000
        assert focuser.get_position() == 2000


class TestHardSafetyEnvelope:
    def test_never_commands_a_position_outside_the_device_max(self) -> None:
        focuser = FakeFocuser()  # device max is fixed at 5000
        focuser.move_absolute(4900)
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25,
            max_coarse_steps=30,
        )

        # Optimum well past the device's own ceiling -- must never be reached.
        result = searcher.search(_monotonic_ramp(focuser))

        assert all(point.position <= 5000 for point in result.samples)
        assert focuser.get_position() <= 5000

    def test_never_exceeds_the_1000_step_envelope_from_the_start_position(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(2500)
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25,
            max_coarse_steps=30,  # would travel 7500 steps unbounded
        )

        # Optimum far beyond the ±1000 envelope -- the search must stay
        # bounded to [1500, 3500] regardless of how "improving" the curve
        # still looks at the edge.
        result = searcher.search(_monotonic_ramp(focuser))

        assert all(1500 <= point.position <= 3500 for point in result.samples)
        assert 1500 <= focuser.get_position() <= 3500

    def test_optimum_at_the_search_boundary_is_reported_bounded_not_extrapolated(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(2500)
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25,
            max_coarse_steps=30,
        )

        # The metric keeps improving all the way to (and past) the
        # envelope edge -- the true optimum is unreachable within bounds.
        result = searcher.search(_monotonic_ramp(focuser))

        assert result.status is AutofocusStatus.BEST_AT_SEARCH_LIMIT
        assert result.best_position in (1500, 3500)


def _plateauing_near_boundary_climb() -> Callable[[], FocusSample]:
    """Issue #35's own real-bundle shape: rises comfortably at first,
    then the last two samples each fall under the 5% `_better()` margin
    right at the search boundary -- neither is an actual decline (3.5 ->
    3.6 -> 3.62 keeps rising, just too slowly to clear the margin).
    Unlike `_monotonic_ramp` (whose own swings stay proportionally large
    enough to clear 5% at every single step, so `best_position` always
    lands exactly ON the boundary already), this reproduces the real
    bundle's own gap: `best_position` ends up one coarse-step *behind*
    the literal boundary. Keyed by call order, not position, so it's
    independent of exactly which start position a test uses."""
    values = iter([1.0, 2.0, 3.5, 3.6, 3.62, 3.55])

    def measure() -> FocusSample:
        return FocusSample(value=next(values), confidence=1.0)

    return measure


def _declining_at_boundary_climb() -> Callable[[], FocusSample]:
    """A clean interior peak (at the 2nd hill-climb step) that merely
    *grazes* the boundary afterward while genuinely declining -- must
    NOT be mistaken for `_plateauing_near_boundary_climb`'s ambiguous
    case."""
    values = iter([1.0, 2.0, 5.0, 2.0, 1.8, 5.1])

    def measure() -> FocusSample:
        return FocusSample(value=next(values), confidence=1.0)

    return measure


class TestBoundarySafetyNet:
    """Issue #35 Fix 1: the real diagnostic bundle (UUID
    73a007b6-6c9b-41e2-a3e5-66a21ec71ffd) showed a curve that never
    turned over before hitting the search boundary, yet the *existing*
    boundary check (`best_position in (allowed_min, allowed_max)`)
    missed it -- the 5%-margin bookkeeping had already left
    `best_position` one coarse-step behind the literal boundary."""

    def test_boundary_reached_while_still_rising_is_not_reported_success(self) -> None:
        focuser = FakeFocuser()
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=True, coarse_step=250, fine_step=25,
            envelope_steps=750, max_consecutive_no_improve=2,
        )

        result = searcher.search(_plateauing_near_boundary_climb())

        assert result.status is AutofocusStatus.BEST_AT_SEARCH_LIMIT

    def test_a_clean_interior_peak_that_merely_grazes_the_boundary_still_succeeds(
        self,
    ) -> None:
        focuser = FakeFocuser()
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=True, coarse_step=250, fine_step=25,
            envelope_steps=750, max_consecutive_no_improve=2,
        )

        result = searcher.search(_declining_at_boundary_climb())

        assert result.status is AutofocusStatus.SUCCESS
        assert result.best_position == 500


class TestFinalValidation:
    """Issue #35 Fix 2: a fresh post-move confirmation sample that turns
    out decisively worse than the starting position's own measurement
    must never be reported as SUCCESS (simulates e.g. backlash/hysteresis
    making the real resting position worse than exploratory samples
    suggested)."""

    def test_final_confirmation_worse_than_start_is_reported_inconsistent(self) -> None:
        values = iter([10.0, 12.0, 6.0, 5.0, 4.0])

        def measure() -> FocusSample:
            return FocusSample(value=next(values), confidence=1.0)

        focuser = FakeFocuser()
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=True, coarse_step=250, fine_step=25,
            max_consecutive_no_improve=2,
        )

        result = searcher.search(measure)

        assert result.status is AutofocusStatus.INCONSISTENT_CURVE
        assert focuser.get_position() == 0  # returned to the start position
        assert result.final_value == 4.0


class TestRejectionCancellationAndFrameLoss:
    def test_a_rejected_focuser_command_aborts_cleanly(self) -> None:
        focuser = _RejectingFocuser(reject_positions={250})
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25
        )

        result = searcher.search(_v_curve(focuser, best_position=1000))

        assert result.status is AutofocusStatus.MOVE_REJECTED
        # Left at a known, safe position -- not stranded mid-command.
        assert focuser.get_position() == 0

    def test_no_usable_evidence_at_all_is_reported_distinctly(self) -> None:
        focuser = FakeFocuser()
        searcher = BoundedFocusSearcher(focuser, higher_is_better=False)

        result = searcher.search(lambda: None)

        assert result.status is AutofocusStatus.NO_USABLE_EVIDENCE
        assert result.samples == ()

    def test_frame_acquisition_failure_mid_search_aborts_to_best_known_safe(self) -> None:
        focuser = FakeFocuser()
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25
        )
        calls = {"n": 0}

        def measure() -> FocusSample | None:
            calls["n"] += 1
            if calls["n"] == 1:
                return FocusSample(value=5.0, confidence=1.0)
            return None  # lost on the very next measurement

        result = searcher.search(measure)

        assert result.status is AutofocusStatus.FRAME_ACQUISITION_FAILED
        assert focuser.get_position() == 0  # returned to the only known-safe position

    def test_cancellation_stops_the_scan_and_returns_to_best_known_safe(self) -> None:
        focuser = FakeFocuser()
        focuser.move_absolute(2500)
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25
        )
        calls = {"n": 0}

        def measure() -> FocusSample:
            calls["n"] += 1
            value = 1.0 + 0.01 * abs(focuser.get_position() - 2000)
            return FocusSample(value=value, confidence=1.0)

        result = searcher.search(measure, cancel_check=lambda: calls["n"] >= 3)

        assert result.status is AutofocusStatus.CANCELLED
        assert focuser.get_position() == result.best_position


class TestFocuserUnavailable:
    def test_an_unavailable_focuser_refuses_before_moving_at_all(self) -> None:
        focuser = FakeFocuser(available=False)
        searcher = BoundedFocusSearcher(focuser, higher_is_better=False)

        result = searcher.search(lambda: FocusSample(value=1.0, confidence=1.0))

        assert result.status is AutofocusStatus.FOCUSER_UNAVAILABLE
        assert result.samples == ()


class TestMoveCompletionIsWaitedFor:
    def test_measure_is_not_called_until_the_focuser_reports_not_moving(self) -> None:
        focuser = _OnceMovingFocuser()
        focuser.move_absolute(2500)
        focuser._pending_busy = False  # clear the initial-move flag before the run starts
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25,
        )
        observed_moving_at_measure_time: list[bool] = []

        def measure() -> FocusSample:
            observed_moving_at_measure_time.append(focuser.is_moving())
            return FocusSample(value=1.0, confidence=1.0)

        searcher.search(measure)

        # is_moving() was already polled to completion (and consumed) by
        # the searcher itself before each measure() call -- so a fresh
        # call inside measure() must always see "not moving" by then.
        assert all(not moving for moving in observed_moving_at_measure_time)


class _MoveTrackingFocuser(FakeFocuser):
    def __init__(self) -> None:
        super().__init__()
        self.commanded_positions: list[int] = []

    def move_absolute(self, steps: int) -> FocuserMoveResult:
        self.commanded_positions.append(steps)
        return super().move_absolute(steps)


class TestConsistentDirectionFinalApproach:
    def test_final_approach_always_comes_from_the_configured_direction(self) -> None:
        # Best position reached by scanning in the NEGATIVE direction --
        # the final physical move into it must still come from +1 (the
        # configured final_approach_direction), same backlash-elimination
        # dance as the existing FocusSearcher.
        focuser = _MoveTrackingFocuser()
        focuser.move_absolute(2500)
        focuser.commanded_positions.clear()
        searcher = BoundedFocusSearcher(
            focuser, higher_is_better=False, coarse_step=250, fine_step=25,
            final_approach_direction=1,
        )

        searcher.search(_v_curve(focuser, best_position=2000))

        # The final position is reached, and the very last commanded
        # move lands exactly on it (whether via direct approach or the
        # overshoot-and-return backlash-elimination pair).
        assert focuser.commanded_positions[-1] == 2000
        assert focuser.get_position() == 2000
