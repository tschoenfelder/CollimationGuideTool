"""S6.6b (S6.7 follow-up): the GUI thread snapshots the camera exposure and hands it to the
worker-side fresh-frame waits as `exposure_hint` -- for guide-assisted reacquisition
(MainWindow, Guide camera) and autofocus (FocuserPanel, Main camera) -- so a first frame of a
long exposure is waited for instead of ending in fresh_frame_timeout. Widgets are read on the
GUI thread only; the worker reads the snapshot."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.application.autofocus_controller import ExposureControl
from collimation_tool.ui import focuser_panel as focuser_module
from collimation_tool.ui.focuser_panel import FocuserPanel
from collimation_tool.ui.main_window import MainWindow


def test_autofocus_gets_the_exposure_snapshotted_when_it_starts(
    qapp: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class _Controller:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            built.append(kwargs)

    monkeypatch.setattr(focuser_module, "AutofocusController", _Controller)
    exposure = [8000.0]
    panel = FocuserPanel(
        FakeFocuser(),
        get_frame=lambda: np.zeros((8, 8), dtype=np.float32),
        wait_for_frame=lambda _reference, _timeout: None,  # type: ignore[arg-type,return-value]
        set_auto_exposure_paused=lambda _paused: None,
        exposure_control=ExposureControl(get=lambda: (exposure[0], 100), set=lambda _e, _g: None),
    )
    panel._connect_button.setChecked(True)
    monkeypatch.setattr(panel._autofocus_runner, "submit", lambda *_a: False)

    panel._on_auto_focus_clicked()
    exposure[0] = 50.0  # a later change on the GUI side is not what the run was started with

    assert len(built) == 1
    assert built[0]["exposure_hint"]() == 8.0
    panel.stop()


def test_reacquisition_gets_the_guide_exposure_snapshot(qapp: object) -> None:
    window = MainWindow(
        ReplayCamera.from_arrays([np.full((60, 80), 100.0, dtype=np.float32)], cycle=True),
        device_lister=lambda: [],
        focuser=FakeFocuser(),
        mount=FakeMountPark(start_parked=False),
        pulse_mount=FakeMountAdapter(),
    )
    window._right_panel.current_exposure_gain = lambda: (8000.0, 100)  # type: ignore[method-assign]
    window._snapshot_guide_exposure()  # what the GUI-thread timer does
    window._last_calibration_result = object()  # type: ignore[assignment]
    window._last_prior_a = object()  # type: ignore[assignment]
    seen: dict[str, Any] = {}

    class _Acquisition:
        def attempt_guide_reacquisition(self, *_args: object, **kwargs: object) -> str:
            seen.update(kwargs)
            return "done"

    window._reacquire_via_guide(_Acquisition(), None)  # type: ignore[arg-type]

    assert seen["exposure_hint"]() == 8.0
    assert window._exposure_snapshot_timer.isActive()

    window.close()  # review C5: the GUI-side timers stop with the window

    assert not window._exposure_snapshot_timer.isActive()
    assert not window._tracking_reenforce_timer.isActive()
