"""Operator (2026-11-27): the SCT is collimated with 3 screws, adjusted one by one. The tool
learns the line along which each screw moves the mismatch and shows, per screw, how far to
turn it: not at all up to 1/8 turn; near collimation only the smallest movement. Which way to
turn is the operator's call (it inverts across focus)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
from collimation_tool.domain.collimation_measurement import CircleEllipseFit, DonutMeasurement
from collimation_tool.domain.screw_guidance import (
    SMALLEST_MOVEMENT,
    Screw,
    ScrewHint,
    ScrewModel,
)
from collimation_tool.ui.collimation_overlay import ARROW_COLOR, SCREW_COLORS
from collimation_tool.ui.live_view import LiveViewLabel
from collimation_tool.ui.screw_panel import READINGS_PER_PICTURE, ScrewPanel
from PySide6.QtGui import QColor, QImage

#: What a 1/8 turn of each screw (one way) does to the offset (shares of the ring radius):
#: three screws 120 degrees apart.
_TRUE = {
    screw: (0.04 * math.cos(math.radians(angle)), 0.04 * math.sin(math.radians(angle)))
    for screw, angle in ((Screw.RED, 0.0), (Screw.GREEN, 120.0), (Screw.BLUE, 240.0))
}


def _turned(offset: tuple[float, float], screw: Screw, steps: float) -> tuple[float, float]:
    return (offset[0] + steps * _TRUE[screw][0], offset[1] + steps * _TRUE[screw][1])


def _taught() -> ScrewModel:
    model = ScrewModel()
    for screw in Screw:
        before = (0.1, 0.05)
        learned, _text = model.observe(
            screw, size=1.0, before=before, after=_turned(before, screw, 1.0)
        )
        assert learned
    return model


def _by_screw(hints: list[ScrewHint]) -> dict[Screw, ScrewHint]:
    return {hint.screw: hint for hint in hints}


class TestScrewModel:
    def test_turns_either_way_teach_the_same_line(self) -> None:
        model = ScrewModel()

        first = model.observe(
            Screw.RED, size=0.5, before=(0.1, 0.0), after=_turned((0.1, 0.0), Screw.RED, -0.5)
        )
        # The other way round -- or the same turn on the other side of focus.
        second = model.observe(
            Screw.RED, size=1.0, before=(0.1, 0.0), after=_turned((0.1, 0.0), Screw.RED, 1.0)
        )

        assert first[0] and second[0]
        response = model.response(Screw.RED)
        assert response is not None
        assert math.hypot(*response) == pytest.approx(0.04)
        assert response[1] == pytest.approx(0.0)

    def test_no_suggestion_names_a_turning_direction(self) -> None:
        texts = " ".join(hint.text() for hint in _taught().hints((0.2, 0.05)))

        assert "clockwise" not in texts
        assert "arrow" in texts

    def test_a_large_mismatch_asks_for_the_screw_that_opposes_it_at_most_an_eighth(
        self,
    ) -> None:
        hints = _by_screw(_taught().hints((0.2, 0.0)))

        red = hints[Screw.RED]
        assert red.best
        assert (red.strength, red.amount) == (1.0, "1/8 turn")
        # The arrow points the way the hole should move: against the mismatch.
        assert red.direction == pytest.approx((-1.0, 0.0))
        assert sum(hint.best for hint in hints.values()) == 1

    def test_turning_as_suggested_reaches_collimation(self) -> None:
        model = _taught()
        offset = (0.12, -0.07)
        steps = {"1/8 turn": 1.0, "1/16 turn": 0.5, SMALLEST_MOVEMENT: 0.15}

        for _ in range(20):
            if math.hypot(*offset) < 0.01:
                break
            best = next(hint for hint in model.hints(offset) if hint.best)
            # The operator turns so that the hole moves along the arrow.
            true = _TRUE[best.screw]
            along = true[0] * best.direction[0] + true[1] * best.direction[1]
            offset = _turned(offset, best.screw, math.copysign(steps[best.amount], along))

        assert math.hypot(*offset) < 0.01

    def test_near_collimation_only_the_smallest_movement_is_suggested(self) -> None:
        turned = [hint for hint in _taught().hints((0.02, 0.0)) if hint.strength > 0]

        assert {hint.amount for hint in turned} == {SMALLEST_MOVEMENT}
        assert max(hint.strength for hint in turned) <= 0.25  # a short arrow

    def test_a_screw_that_cannot_help_is_left_alone(self) -> None:
        red = _by_screw(_taught().hints((0.0, 0.1)))[Screw.RED]

        assert (red.strength, red.amount) == (0.0, "leave it")

    def test_a_turn_without_measurable_change_or_across_the_known_line_is_not_learned(
        self,
    ) -> None:
        model = _taught()

        still = model.observe(Screw.RED, size=1.0, before=(0.1, 0.0), after=(0.101, 0.0))
        across = model.observe(Screw.RED, size=1.0, before=(0.1, 0.0), after=(0.1, 0.05))

        assert not still[0]
        assert not across[0]
        assert model.turns_learned(Screw.RED) == 1

    def test_unreadable_stored_entries_are_skipped(self) -> None:
        model = ScrewModel()

        model.load_data({"red": [[0.04, 0.0], ["x", 1], [0.0, 0.0]], "green": 5})

        assert model.turns_learned(Screw.RED) == 1
        assert model.turns_learned(Screw.GREEN) == 0


def _measurement(offset: tuple[float, float]) -> DonutMeasurement:
    outer = CircleEllipseFit(960.0, 540.0, 400.0, 400.0, 0.0, 0.9)
    dx, dy = offset[0] * 400.0, offset[1] * 400.0
    inner = CircleEllipseFit(960.0 + dx, 540.0 + dy, 130.0, 130.0, 0.0, 0.9)
    return DonutMeasurement(outer, inner, dx, dy, math.hypot(dx, dy), 0.0, 0.9)


def _feed(panel: ScrewPanel, offset: tuple[float, float]) -> None:
    for _ in range(READINGS_PER_PICTURE):
        panel.add_measurement(_measurement(offset))


def test_the_panel_learns_a_turn_between_two_pictures_and_keeps_it(
    qapp: object, tmp_path: Path
) -> None:
    store = tmp_path / "screws.json"
    model = ScrewModel()
    panel = ScrewPanel(model, store_path=store)
    assert not panel._turn_buttons[Screw.RED].isEnabled()
    panel._remember_button.click()
    _feed(panel, (0.1, 0.05))

    panel._turn_buttons[Screw.GREEN].click()
    panel.add_measurement(None)  # a frame without a donut is skipped
    _feed(panel, _turned((0.1, 0.05), Screw.GREEN, 1.0))

    assert model.response(Screw.GREEN) == pytest.approx(_TRUE[Screw.GREEN])
    assert "learned" in panel.status_text
    restored = ScrewModel()
    ScrewPanel(restored, store_path=store)
    assert restored.response(Screw.GREEN) == pytest.approx(_TRUE[Screw.GREEN])

    panel._forget_button.click()

    assert model.turns_learned(Screw.GREEN) == 0


def _has(image: QImage, color: QColor, x0: int, x1: int, y0: int, y1: int) -> bool:
    pixels = np.array(
        [
            [image.pixelColor(x, y).getRgb()[:3] for x in range(x0, x1)]  # type: ignore[index]
            for y in range(y0, y1)
        ]
    )
    wanted = np.array([color.red(), color.green(), color.blue()])
    return bool((np.abs(pixels - wanted).max(axis=2) <= 30).any())


def test_the_view_draws_a_yellow_mismatch_arrow_and_coloured_screw_arrows(
    qapp: object,
) -> None:
    label = LiveViewLabel()
    label.resize(960, 540)
    label.screw_advice = _taught().hints

    label.set_stretched_frame(
        np.zeros((1080, 1920), dtype=np.uint8), measurement=_measurement((0.2, 0.0))
    )

    image = label.pixmap().toImage()
    centre_x, centre_y = 480, 270
    # Mismatch to the right in yellow; red (the screw to turn) points left, against it.
    assert _has(image, ARROW_COLOR, centre_x + 40, centre_x + 80, centre_y - 6, centre_y + 7)
    assert _has(
        image, SCREW_COLORS[Screw.RED], centre_x - 90, centre_x - 50, centre_y - 6, centre_y + 7
    )
    for screw in (Screw.GREEN, Screw.BLUE):
        assert _has(image, SCREW_COLORS[screw], centre_x - 120, centre_x + 120, 140, 400)
