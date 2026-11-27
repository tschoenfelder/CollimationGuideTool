"""Learn what each of the SCT's three collimation screws does, and suggest the next turn.

Operator (2026-11-27): the screws are adjusted one by one; each should get its own arrow that
points the way it moves the mismatch, its length saying how much to turn it -- from not at
all up to the most, which is 1/8 turn per step even on a strongly decollimated telescope; near
collimation only the smallest possible movement.

All offsets are shares of the outer ring radius (hole centre minus ring centre, x right,
y down), so what is learned holds whatever the defocus. No Qt, no device access.
"""

from __future__ import annotations

import enum
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

Offset = tuple[float, float]


class Screw(enum.StrEnum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"


#: Turn sizes the operator can report, in units of the largest step (1/8 turn).
TURN_SIZES: dict[str, float] = {"1/8 turn": 1.0, "1/16 turn": 0.5}
#: A turn must change the offset by at least this share to be learned from (about the
#: measurement noise).
MIN_CHANGE_SHARE = 0.005
#: Below this offset share only the smallest movement is suggested (fine collimation).
FINE_SHARE = 0.03
#: A screw whose useful turn is below this share of 1/8 turn gets no arrow.
_LEAVE_STRENGTH = 0.1
SMALLEST_MOVEMENT = "smallest movement"
#: The longest arrow (strength) a smallest movement gets: its length says "only a little".
_SMALLEST_STRENGTH = 0.25


@dataclass(frozen=True)
class ScrewHint:
    screw: Screw
    clockwise: bool
    #: 0 (leave it) to 1 (the largest step, 1/8 turn).
    strength: float
    #: "1/8 turn" | "1/16 turn" | "smallest movement" | "leave it".
    amount: str
    #: Unit vector: the way this turn moves the hole relative to the ring.
    direction: Offset
    #: The one screw whose suggested turn removes the most mismatch.
    best: bool = False

    def text(self) -> str:
        name = self.screw.value.capitalize()
        if self.strength <= 0:
            return f"{name}: leave it"
        turn = "clockwise" if self.clockwise else "counter-clockwise"
        return f"{name}: {self.amount} {turn}" + (" (best)" if self.best else "")


class ScrewModel:
    def __init__(self) -> None:
        #: Per screw: the offset change each clockwise 1/8 turn caused.
        self._observations: dict[Screw, list[Offset]] = {}

    def forget(self) -> None:
        self._observations.clear()

    def turns_learned(self, screw: Screw) -> int:
        return len(self._observations.get(screw, ()))

    def response(self, screw: Screw) -> Offset | None:
        """The offset change of one clockwise 1/8 turn, or None while unknown."""
        seen = self._observations.get(screw)
        if not seen:
            return None
        return (sum(x for x, _ in seen) / len(seen), sum(y for _, y in seen) / len(seen))

    def observe(
        self, screw: Screw, *, clockwise: bool, size: float, before: Offset, after: Offset
    ) -> tuple[bool, str]:
        """Learn from one turn of `size` (units of 1/8 turn). Returns (learned, why/what)."""
        change = (after[0] - before[0], after[1] - before[1])
        name = screw.value.capitalize()
        if math.hypot(*change) < MIN_CHANGE_SHARE:
            return False, f"{name}: no measurable change -- nothing learned; turn 1/8"
        sign = 1.0 if clockwise else -1.0
        per_step = (sign * change[0] / size, sign * change[1] / size)
        known = self.response(screw)
        if known is not None and known[0] * per_step[0] + known[1] * per_step[1] < 0:
            return False, (
                f"{name}: this turn moved the opposite way than before -- not learned; check "
                "the screw and direction, or press Forget screws"
            )
        self._observations.setdefault(screw, []).append(per_step)
        return True, f"{name}: learned ({self.turns_learned(screw)} turn(s))"

    def hints(self, offset: Offset) -> list[ScrewHint]:
        """For every learned screw: the turn of that screw alone that reduces `offset` most,
        limited to 1/8 turn."""
        hints: list[ScrewHint] = []
        gains: list[float] = []
        for screw in Screw:
            response = self.response(screw)
            if response is None:
                continue
            rx, ry = response
            length = math.hypot(rx, ry)
            # Least squares: the turn t (1/8 units, clockwise positive) minimising |o + t r|.
            turn = -(offset[0] * rx + offset[1] * ry) / (length * length)
            strength = min(1.0, abs(turn))
            clockwise = turn > 0
            sign = 1.0 if clockwise else -1.0
            if strength < _LEAVE_STRENGTH:
                strength, amount = 0.0, "leave it"
            elif math.hypot(*offset) < FINE_SHARE or strength < 0.35:
                amount = SMALLEST_MOVEMENT
            elif strength < 0.75:
                amount = "1/16 turn"
            else:
                amount = "1/8 turn"
            applied = sign * min(1.0, abs(turn)) if strength > 0 else 0.0
            gains.append(
                math.hypot(*offset)
                - math.hypot(offset[0] + applied * rx, offset[1] + applied * ry)
            )
            if amount == SMALLEST_MOVEMENT:
                strength = min(strength, _SMALLEST_STRENGTH)
            hints.append(
                ScrewHint(
                    screw, clockwise, strength, amount,
                    (sign * rx / length, sign * ry / length),
                )
            )
        if hints and max(gains) > 0:
            top = gains.index(max(gains))
            best = hints[top]
            hints[top] = ScrewHint(
                best.screw, best.clockwise, best.strength, best.amount, best.direction, True
            )
        return hints

    def to_data(self) -> dict[str, list[list[float]]]:
        return {
            screw.value: [[x, y] for x, y in seen] for screw, seen in self._observations.items()
        }

    def load_data(self, data: Mapping[str, Any]) -> None:
        """Replace what is known by `data` (from `to_data`); unreadable entries are skipped."""
        self.forget()
        for screw in Screw:
            entries = data.get(screw.value)
            if not isinstance(entries, Sequence):
                continue
            seen = [
                (float(entry[0]), float(entry[1]))
                for entry in entries
                if isinstance(entry, Sequence)
                and len(entry) == 2
                and all(isinstance(v, int | float) and math.isfinite(v) for v in entry)
                and any(v != 0 for v in entry)
            ]
            if seen:
                self._observations[screw] = seen
