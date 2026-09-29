"""Tests for FlowLayout -- field report (2026-09-29): fixed-row QHBoxLayout
splitting, tuned against one platform's measured pixel widths, still
clipped on the real Pi screen (different font metrics). FlowLayout must
wrap child widgets to a new line whenever the container is too narrow to
fit them on one line, and keep them on one line when there's room --
verified by widget *position* (does a wrap actually happen), never by
exact pixel widths (the same fragility this replaces)."""

from __future__ import annotations

from collimation_tool.ui.flow_layout import FlowLayout
from PySide6.QtWidgets import QPushButton, QWidget


def _flow_widget(
    button_count: int = 5, button_width: int = 80
) -> tuple[QWidget, FlowLayout, list[QPushButton]]:
    container = QWidget()
    layout = FlowLayout(container)
    buttons = []
    for _i in range(button_count):
        button = QPushButton("X" * 10)
        button.setFixedSize(button_width, 24)
        layout.addWidget(button)
        buttons.append(button)
    return container, layout, buttons


def test_all_items_share_one_row_when_there_is_enough_width(qapp: object) -> None:
    container, _layout, buttons = _flow_widget(button_count=3, button_width=80)
    container.resize(1000, 200)
    container.show()
    qapp.processEvents()  # type: ignore[attr-defined]

    ys = {button.y() for button in buttons}
    assert len(ys) == 1  # all on the same row
    container.close()


def test_items_wrap_to_a_new_row_when_the_container_is_too_narrow(qapp: object) -> None:
    container, _layout, buttons = _flow_widget(button_count=5, button_width=80)
    container.resize(150, 400)  # room for ~1-2 buttons per row, not all 5
    container.show()
    qapp.processEvents()  # type: ignore[attr-defined]

    ys = {button.y() for button in buttons}
    assert len(ys) > 1  # spread across multiple rows, not clipped on one
    # every button is still fully inside the container's width -- nothing
    # clipped off the right edge the way a plain QHBoxLayout would.
    for button in buttons:
        assert button.x() + button.width() <= container.width()
    container.close()


def test_minimum_size_reflects_the_single_widest_item_not_the_whole_row(qapp: object) -> None:
    """A FlowLayout's own minimumSize() must not demand every item's
    combined width like QHBoxLayout would -- that's the whole point: it
    can always shrink further by wrapping, so nothing it contains should
    force the window wider than its single widest child."""
    container, layout, buttons = _flow_widget(button_count=5, button_width=80)
    total_width = sum(b.width() for b in buttons)
    min_size = layout.minimumSize()
    assert min_size.width() < total_width
    assert min_size.width() >= 80  # at least the one widest item
