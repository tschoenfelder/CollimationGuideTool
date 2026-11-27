"""Keep the star centred in Main while the operator turns collimation screws.

Operator (2026-11-27): every screw change shifts the star, and the RA/Dec directions do not
match the frame's axes, so centring by hand is too much work. This decides, reading by
reading, whether to move the mount: only when the star has come to rest (the operator is not
turning a screw any more) and is off centre. The move itself is solved from the Main
calibration by Mount Align. No Qt, no device access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

#: Shares of the frame height. Off centre by less than this: nothing to do.
CENTERED_FRACTION = 0.07
#: Two readings closer than this: the star is at rest.
STILL_FRACTION = 0.01
#: Share of the measured offset one move corrects (never overshoot on a rough measurement).
MOVE_GAIN = 0.8
#: Moves in a row without reaching the centre before centring gives up.
MAX_MOVES = 6


#: `move_main`'s answer when the remaining offset needs less than the mount's smallest move.
BELOW_SMALLEST_MOVE = "below the mount's smallest move"


class CenteringAction(Enum):
    WAIT = "wait"
    CENTERED = "centered"
    MOVE = "move"
    GIVE_UP = "give_up"


@dataclass(frozen=True)
class CenteringDecision:
    action: CenteringAction
    text: str
    #: MOVE only: how far the image content should move, in frame pixels.
    dx_px: float = 0.0
    dy_px: float = 0.0


class StarCenterer:
    def __init__(self) -> None:
        self._previous: tuple[float, float] | None = None
        self._moves = 0

    def reset(self) -> None:
        self._previous = None
        self._moves = 0

    def moved(self) -> None:
        """A centring move was started: the next reading is compared with none."""
        self._previous = None
        self._moves += 1

    def read(
        self, position: tuple[float, float] | None, frame_shape: tuple[int, int]
    ) -> CenteringDecision:
        """`position`: the star's disc centre in a fresh frame (None = not found)."""
        if position is None:
            self._previous = None
            return CenteringDecision(CenteringAction.WAIT, "no star found in Main")
        height, width = frame_shape
        previous, self._previous = self._previous, position
        dx = (width - 1) / 2.0 - position[0]
        dy = (height - 1) / 2.0 - position[1]
        off = math.hypot(dx, dy)
        if previous is None or math.dist(previous, position) > STILL_FRACTION * height:
            return CenteringDecision(CenteringAction.WAIT, "waiting for the star to come to rest")
        if off <= CENTERED_FRACTION * height:
            self._moves = 0
            return CenteringDecision(CenteringAction.CENTERED, "star centred")
        if self._moves >= MAX_MOVES:
            return CenteringDecision(
                CenteringAction.GIVE_UP,
                f"still {off:.0f} px off centre after {MAX_MOVES} moves -- run the Main "
                "calibration again",
            )
        return CenteringDecision(
            CenteringAction.MOVE,
            f"star {off:.0f} px off centre -- moving the mount",
            dx_px=MOVE_GAIN * dx,
            dy_px=MOVE_GAIN * dy,
        )
