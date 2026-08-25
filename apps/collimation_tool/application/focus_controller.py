"""Autofocus hill-climb: minimize measured FWHM by moving the focuser.

Redesigned from smart_telescope's `services/collimation/{focus_search,
fwhm_focus}.py`. Those were two separate, near-duplicate hill-climbers:
`focus_search.py`'s probe step tested only one direction and assumed the
untested direction was "good" when the first move didn't improve —a
shortcut that existed to avoid wasting a measurement when a soft focuser
limit blocked one direction. `fwhm_focus.py`'s probe tested both
directions properly and had no such soft-limit assumption baked in, plus
a backlash-elimination final-approach-direction correction.

astrotool_core's `FocuserPort` has no soft-limit-detection surface at all
(`move()` returns `None`, `move_absolute()`'s `FocuserMoveResult` never
reports partial/rejected relative moves), so `focus_search.py`'s
soft-limit-driven asymmetric probe has nothing to guard against here.
This module ports `fwhm_focus.py`'s cleaner, symmetric-probe algorithm
once and uses it for both the rough (post-defocus) and final refocus
roles PLAN.md originally split across two classes — see
docs/porting-notes.md.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from astrotool_core.focus.port import FocuserPort

from collimation_tool.domain.focus_metric import classify_focus_quality

FwhmMeasurer = Callable[[], float | None]  # returns None when the star is lost


@dataclass(frozen=True)
class FocusSearchResult:
    reason: str  # "converged" | "star_lost" | "cancelled" | "max_steps"
    quality: str  # "excellent" | "good" | "poor" | "failed"
    initial_fwhm_px: float | None
    best_fwhm_px: float | None
    final_fwhm_px: float | None
    steps_taken: int
    frame_count: int


class FocusSearcher:
    """Coarse hill-climb focus search using relative focuser steps only.

    Never trusts absolute focuser position — every correction is a
    relative `move()`, so behavior is identical whether or not the
    focuser's absolute position readback is meaningful.
    """

    def __init__(
        self,
        focuser: FocuserPort,
        *,
        coarse_step: int = 250,
        fine_step: int = 25,
        max_coarse_steps: int = 20,
        max_consecutive_no_improve: int = 2,
        improvement_fraction: float = 0.05,
        final_approach_direction: int = 1,  # +1 or -1
        excellent_fwhm_px: float = 2.0,
        good_fwhm_px: float = 4.0,
    ) -> None:
        self._focuser = focuser
        self._coarse_step = coarse_step
        self._fine_step = fine_step
        self._max_coarse_steps = max_coarse_steps
        self._max_consecutive_no_improve = max_consecutive_no_improve
        self._improvement_fraction = improvement_fraction
        self._final_dir = final_approach_direction
        self._excellent_fwhm_px = excellent_fwhm_px
        self._good_fwhm_px = good_fwhm_px

    def search(
        self,
        get_fwhm: FwhmMeasurer,
        cancel_check: Callable[[], bool] | None = None,
    ) -> FocusSearchResult:
        frame_count = 0

        def measure() -> float | None:
            nonlocal frame_count
            frame_count += 1
            return get_fwhm()

        initial_fwhm = measure()
        if initial_fwhm is None:
            return self._result("star_lost", None, None, None, 0, frame_count)

        direction, current_pos, best_fwhm, best_pos = self._probe(measure, initial_fwhm)
        if direction is None:
            return self._result("star_lost", initial_fwhm, None, None, 0, frame_count)
        if direction == 0:
            self._focuser.move(-current_pos)
            return self._result("max_steps", initial_fwhm, None, None, 0, frame_count)

        consecutive = 0
        for _ in range(self._max_coarse_steps):
            if cancel_check is not None and cancel_check():
                self._focuser.move(best_pos - current_pos)
                return self._result(
                    "cancelled", initial_fwhm, best_fwhm, None, best_pos, frame_count
                )

            self._focuser.move(direction * self._coarse_step)
            current_pos += direction * self._coarse_step
            fwhm = measure()
            if fwhm is None:
                self._focuser.move(best_pos - current_pos)
                return self._result(
                    "star_lost", initial_fwhm, best_fwhm, None, best_pos, frame_count
                )

            if fwhm < best_fwhm * (1 - self._improvement_fraction):
                best_fwhm, best_pos = fwhm, current_pos
                consecutive = 0
            else:
                consecutive += 1
                if consecutive >= self._max_consecutive_no_improve:
                    break

        self._focuser.move(best_pos - current_pos)
        current_pos = best_pos

        # Backlash elimination: always make the last physical move come from
        # the same configured direction, regardless of which way the scan went.
        if direction != self._final_dir:
            self._focuser.move(-self._final_dir * self._fine_step)
            self._focuser.move(self._final_dir * self._fine_step)

        final_fwhm = measure()
        if final_fwhm is None:
            return self._result(
                "star_lost", initial_fwhm, best_fwhm, None, current_pos, frame_count
            )

        reported_best = min(best_fwhm, final_fwhm)
        return self._result(
            "converged", initial_fwhm, reported_best, final_fwhm, current_pos, frame_count
        )

    def _probe(
        self, measure: Callable[[], float | None], initial_fwhm: float
    ) -> tuple[int | None, int, float, int]:
        """Test both directions once; return (direction, current_pos, best_fwhm, best_pos).

        direction: +1/-1 = that direction improved; 0 = neither improved;
        None = the star was lost during probing.
        """
        self._focuser.move(self._coarse_step)
        fwhm_fwd = measure()
        if fwhm_fwd is None:
            return None, self._coarse_step, initial_fwhm, 0
        if fwhm_fwd < initial_fwhm * (1 - self._improvement_fraction):
            return 1, self._coarse_step, fwhm_fwd, self._coarse_step

        self._focuser.move(-2 * self._coarse_step)
        fwhm_back = measure()
        if fwhm_back is None:
            return None, -self._coarse_step, initial_fwhm, 0
        if fwhm_back < initial_fwhm * (1 - self._improvement_fraction):
            return -1, -self._coarse_step, fwhm_back, -self._coarse_step

        self._focuser.move(self._coarse_step)  # back to the starting position
        return 0, 0, initial_fwhm, 0

    def _result(
        self,
        reason: str,
        initial_fwhm: float | None,
        best_fwhm: float | None,
        final_fwhm: float | None,
        steps_taken: int,
        frame_count: int,
    ) -> FocusSearchResult:
        if reason != "converged" or best_fwhm is None:
            quality = "failed"
        else:
            quality = classify_focus_quality(
                best_fwhm,
                excellent_fwhm_px=self._excellent_fwhm_px,
                good_fwhm_px=self._good_fwhm_px,
            ).tier
        return FocusSearchResult(
            reason=reason,
            quality=quality,
            initial_fwhm_px=initial_fwhm,
            best_fwhm_px=best_fwhm,
            final_fwhm_px=final_fwhm,
            steps_taken=steps_taken,
            frame_count=frame_count,
        )
