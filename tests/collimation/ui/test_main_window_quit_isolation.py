"""#52 K03 (S6.2): `MainWindow.closeEvent` stopped every panel in sequence without isolation --
one raising `stop()` skipped every panel after it, so e.g. a mount whose tracking-off could not
be confirmed left the focuser, filter wheel and Mount Align connections open past quit.

One MainWindow (the isolation lives in closeEvent itself); each panel's real `stop()` still
runs so no timer outlives the test."""

from __future__ import annotations

import sys
from collections.abc import Callable

import numpy as np
import pytest
from astrotool_core.camera.replay_camera import ReplayCamera
from collimation_tool.ui.main_window import MainWindow
from PySide6.QtGui import QCloseEvent


def _window() -> MainWindow:
    image = np.full((60, 80), 100.0, dtype=np.float32)
    return MainWindow(ReplayCamera.from_arrays([image], cycle=True), device_lister=lambda: [])


def _record(
    monkeypatch: pytest.MonkeyPatch,
    panel: object,
    name: str,
    calls: list[str],
    *,
    raises: Exception | None = None,
) -> None:
    real: Callable[[], None] = panel.stop  # type: ignore[attr-defined]

    def stop() -> None:
        real()
        calls.append(name)
        if raises is not None:
            raise raises

    monkeypatch.setattr(panel, "stop", stop)


def test_one_failing_panel_stop_does_not_skip_the_others(
    qapp: object, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    window = _window()
    calls: list[str] = []
    panels = {
        "left": window._left_panel,
        "right": window._right_panel,
        "focuser": window._focuser_panel,
        "mount": window._mount_panel,
        "test_move": window._test_move_panel,
        "fine_collimation": window._fine_collimation_panel,
    }
    failure = RuntimeError("OnStep still reports motion after stop")
    for name, panel in panels.items():
        _record(monkeypatch, panel, name, calls, raises=failure if name == "left" else None)
    for index, wheel_panel in enumerate(window._filter_wheel_panels):
        _record(monkeypatch, wheel_panel, f"wheel{index}", calls)

    with caplog.at_level("WARNING"):
        window.closeEvent(QCloseEvent())  # must not raise

    wheels = {f"wheel{i}" for i in range(len(window._filter_wheel_panels))}
    assert set(calls) == set(panels) | wheels
    logged = [r for r in caplog.records if "still reports motion" in r.getMessage()]
    assert len(logged) == 1


def test_an_internal_error_in_a_panel_stop_reaches_the_incident_hook(
    qapp: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S6.2 review: it used to escape closeEvent into sys.excepthook (#10 incident capture)."""
    hook: list[type[BaseException]] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, _v, _tb: hook.append(t))
    window = _window()
    calls: list[str] = []
    _record(monkeypatch, window._focuser_panel, "focuser", calls, raises=TypeError("bad call"))
    _record(monkeypatch, window._mount_panel, "mount", calls)

    window.closeEvent(QCloseEvent())

    assert calls == ["focuser", "mount"]
    assert hook == [TypeError]
