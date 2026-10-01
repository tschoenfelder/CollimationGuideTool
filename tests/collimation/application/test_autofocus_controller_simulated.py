"""Issue #51 component workflow: AutofocusController (star mode) over the
production `OnStepFocuserAdapter` (simulated OnStep focuser, backlash
included) and the production `TouptekCameraAdapter` (simulated SDK); every
exposure and focuser move takes fake time."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
from astrotool_core.onstep import (
    OnStepFocuserAdapter,
)
from astrotool_core.testing import (
    CameraFrameSource,
    FocuserScenario,
    OnStepScenario,
    SimulatedIndiFocuser,
    SimulatedToupcamSdk,
    install_simulated_toupcam,
    make_simulated_onstep_connection,
    single_star_image,
)
from astrotool_core.timing import FakeClock
from collimation_tool.application.autofocus_controller import AutofocusController, AutofocusMode
from collimation_tool.application.autofocus_search import AutofocusStatus


class TestAutofocusWorkflow:
    """AutofocusController (star mode) over the production OnStep focuser
    adapter and the production ToupTek adapter, both simulated: the star's
    blur follows the simulated focuser's OPTICAL position (backlash
    included), and every exposure and focuser move takes fake time."""

    _BEST = 5300

    def _run(self, *, backlash: int) -> tuple[object, SimulatedIndiFocuser, FakeClock]:
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(
            OnStepScenario(
                focuser=FocuserScenario(position=5000, steps_per_s=400.0, backlash_steps=backlash)
            ),
            clock=clock,
        )
        focuser = OnStepFocuserAdapter(connection)
        focuser.connect()
        sim_focuser = made[0].focuser

        def scene(_start: float, _end: float) -> np.ndarray:
            optical = sim_focuser.optical_position or 0.0
            sigma = 1.5 + abs(optical - self._BEST) / 120.0
            return single_star_image(
                (64, 96), x=48.0, y=32.0, peak=6000.0 / (sigma / 1.5) ** 2, sigma=sigma
            )

        sdk = SimulatedToupcamSdk(clock=clock)
        device = sdk.add_camera("G3M678M", scene=scene)
        monkeypatch = pytest.MonkeyPatch()
        try:
            install_simulated_toupcam(monkeypatch, sdk)
            camera = TouptekCameraAdapter(camera_id=device.id)
            camera.connect()
            frames = CameraFrameSource(camera, clock, exposure_s=1.0)
            controller = AutofocusController(
                focuser,
                get_frame=lambda: frames.next_frame(0.0).pixels,  # type: ignore[union-attr]
                wait_for_frame=frames.waiter(),
                set_auto_exposure_paused=lambda _paused: None,
                star_edge_margin_px=8,
                clock=clock,
            )
            result = controller.run(AutofocusMode.STAR)
        finally:
            monkeypatch.undo()
        return result, sim_focuser, clock

    def test_finds_focus_on_the_simulated_rig(self) -> None:
        result, sim_focuser, clock = self._run(backlash=0)
        assert result.status is AutofocusStatus.SUCCESS, result  # type: ignore[attr-defined]
        assert abs(result.best_position - self._BEST) <= 50  # type: ignore[attr-defined]
        assert sim_focuser.position == result.best_position  # type: ignore[attr-defined]
        assert sim_focuser.move_log[-1] == result.best_position  # type: ignore[attr-defined]
        stops = [5000, *sim_focuser.move_log]
        travel_s = sum(abs(b - a) for a, b in zip(stops, stops[1:], strict=False)) / 400.0
        assert clock.monotonic() > travel_s + 3 * 1.0  # moves + >=3 exposures, all fake time

    def test_the_final_approach_absorbs_backlash(self) -> None:
        result, sim_focuser, _ = self._run(backlash=60)
        assert result.status is AutofocusStatus.SUCCESS, result  # type: ignore[attr-defined]
        assert sim_focuser.optical_position is not None
        assert abs(sim_focuser.optical_position - self._BEST) <= 100
