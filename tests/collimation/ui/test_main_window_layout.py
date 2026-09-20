"""Issue #36: MainWindow layout -- frames always visible, secondary controls
in tabs, everything fits the minimum supported desktop (1280x720)."""

from __future__ import annotations

import pytest
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.filter_wheel.fake_filter_wheel import FakeFilterWheel
from astrotool_core.filter_wheel.registry import FilterWheelAssignment
from astrotool_core.testing.frame_factory import single_star_image
from collimation_tool.ui.main_window import MainWindow
from PySide6.QtWidgets import QScrollArea, QSplitter, QTabWidget, QWidget

MIN_W, MIN_H = 1280, 720
# Usable area on a 1280x720 desktop after title bar / taskbar.
USABLE_H = 680


def _window() -> MainWindow:
    image = single_star_image(
        (240, 320), x=160.0, y=120.0, peak=3000.0, sigma=2.0, background=100.0
    )
    return MainWindow(
        ReplayCamera.from_arrays([image], cycle=True),
        device_lister=lambda: [],
        filter_wheels=[
            FilterWheelAssignment("efw1", "ToupTek EFW 1", ("Main", "OAG"), FakeFilterWheel())
        ],
    )


def _ancestors(widget: QWidget) -> list[QWidget]:
    out: list[QWidget] = []
    parent = widget.parentWidget()
    while parent is not None:
        out.append(parent)
        parent = parent.parentWidget()
    return out


def _panels(window: MainWindow) -> dict[str, QWidget]:
    return {
        "focuser": window._focuser_panel,
        "efw": window._filter_wheel_panels[0],
        "mount": window._mount_panel,
        "test_move": window._test_move_panel,
        "fine": window._fine_collimation_panel,
    }


@pytest.fixture
def shown(qapp: object) -> MainWindow:
    window = _window()
    window.resize(MIN_W, USABLE_H)
    window.show()
    qapp.processEvents()  # type: ignore[attr-defined]
    return window


def test_minimum_size_fits_a_1280x720_desktop(qapp: object) -> None:
    window = _window()
    hint = window.minimumSizeHint()
    assert hint.width() <= MIN_W
    assert hint.height() <= USABLE_H


def test_both_frames_are_visible_and_useful_at_minimum_size(shown: MainWindow) -> None:
    for panel in (shown._left_panel, shown._right_panel):
        assert panel.isVisible()
        assert panel.width() >= 320
        assert panel.height() >= 240
        top_left = panel.mapTo(shown, panel.rect().topLeft())
        bottom_right = panel.mapTo(shown, panel.rect().bottomRight())
        assert bottom_right.x() <= shown.width()
        assert bottom_right.y() <= shown.height()
        assert top_left.y() >= 0


def test_frames_are_not_pushed_below_the_controls(shown: MainWindow) -> None:
    frames_y = shown._left_panel.mapTo(shown, shown._left_panel.rect().topLeft()).y()
    assert frames_y < shown.height() // 3


def test_every_control_panel_is_in_a_tab_inside_a_scroll_area(shown: MainWindow) -> None:
    for name, panel in _panels(shown).items():
        ancestors = _ancestors(panel)
        assert any(isinstance(a, QTabWidget) for a in ancestors), name
        assert any(isinstance(a, QScrollArea) for a in ancestors), name


def test_frames_live_in_a_splitter_beside_the_controls(shown: MainWindow) -> None:
    ancestors = _ancestors(shown._left_panel)
    assert any(isinstance(a, QSplitter) for a in ancestors)
    assert not any(isinstance(a, QTabWidget) for a in ancestors)


def test_each_panel_becomes_visible_when_its_tab_is_selected(
    shown: MainWindow, qapp: object
) -> None:
    tabs = shown.findChild(QTabWidget)
    assert tabs is not None
    for name, panel in _panels(shown).items():
        for index in range(tabs.count()):
            tabs.setCurrentIndex(index)
            qapp.processEvents()  # type: ignore[attr-defined]
            if panel.isVisible():
                break
        assert panel.isVisible(), name


def test_switching_tabs_does_not_change_the_frame_size(shown: MainWindow, qapp: object) -> None:
    tabs = shown.findChild(QTabWidget)
    assert tabs is not None
    sizes = set()
    for index in range(tabs.count()):
        tabs.setCurrentIndex(index)
        qapp.processEvents()  # type: ignore[attr-defined]
        sizes.add((shown._left_panel.width(), shown._left_panel.height()))
    assert len(sizes) == 1


def test_focus_tab_groups_the_focuser_and_the_shared_filter_wheel(shown: MainWindow) -> None:
    tabs = shown.findChild(QTabWidget)
    assert tabs is not None
    focus_tab = next(
        page
        for page in (tabs.widget(i) for i in range(tabs.count()))
        if page is not None and shown._focuser_panel in _children(page)
    )
    assert shown._filter_wheel_panels[0] in _children(focus_tab)
    assert len(shown._filter_wheel_panels) == 1  # one physical wheel, one control


def _children(widget: QWidget) -> list[QWidget]:
    return widget.findChildren(QWidget)
