"""Issue #51 component workflow: Mount Align calibration --
MountTestMovePanel -> MountTestMoveRunner -> production OnStep park/pulse
adapters -> OnStepConnection -> simulated OnStep controller.

Acceptance gap pending S3b: MountTestMoveRunner still sleeps and stamps on the
real clock, so this test drains the runner with a bounded real-time poll
(5 ms steps, 120 s safety deadline; ~5.5 s per run, like the existing Mount
Align tests) and stamps frames with `time.monotonic()`. The simulated mount
itself runs on fake time. Once S3b injects the clock, this becomes a pure
fake-time test.
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount.movement_sizing import CameraGeometry
from astrotool_core.onstep import (
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
)
from astrotool_core.testing import (
    OnStepScenario,
    make_simulated_onstep_connection,
)
from astrotool_core.timing import FakeClock
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel
from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner

_SETTINGS = MountAlignmentSettings(
    settle_ms=0, frame_settle_ms=0, stability_sample_interval_s=0.0, stability_timeout_s=3.0
)
_LEFT = CameraGeometry("left", 200, 120, 3.0)
_RIGHT = CameraGeometry("right", 300, 100, 12.0)


def _texture(seed: int, shape: tuple[int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random(shape).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0, dtype=np.float32)


class TestMountAlignWorkflow:
    """Mount Align calibration: MountTestMovePanel -> MountTestMoveRunner ->
    production OnStep park/pulse adapters -> OnStepConnection -> simulated
    controller (axis moves take fake time at 2 deg/s). Each camera sees a
    textured sky shifted by what the simulated mount really moved."""

    def test_calibration_runs_end_to_end_on_the_simulated_controller(self, qapp: object) -> None:
        clock = FakeClock()
        connection, made = make_simulated_onstep_connection(OnStepScenario(), clock=clock)
        park = OnStepMountParkAdapter(connection)
        mount = OnStepMountPulseAdapter(connection)
        park.connect()
        mount.connect()
        client = made[0]
        ha0, dec0 = client.ha_deg, client.dec_deg
        cameras = {"left": _LEFT, "right": _RIGHT}
        base = {
            key: _texture(i, (c.height_px, c.width_px))
            for i, (key, c) in enumerate(cameras.items(), start=1)
        }

        def frame(key: str) -> np.ndarray:
            scale = cameras[key].arcsec_per_px or 1.0
            dx = (client.ha_deg - ha0) * 3600.0 / scale
            dy = (client.dec_deg - dec0) * 3600.0 / scale
            return np.roll(np.roll(base[key], round(dy), axis=0), round(dx), axis=1)

        def waiter(key: str):  # type: ignore[no-untyped-def]  # noqa: ANN202
            def wait(_reference: float, _timeout: float) -> FrameAcquisitionResult:
                # real-clock stamp: the runner still references time.monotonic() (S3b)
                return FrameAcquisitionResult(
                    FrameAcquisitionStatus.OK,
                    DeliveredFrame(frame(key), time.monotonic(), 0.01),
                )

            return wait

        panel = MountTestMovePanel(
            mount,
            mount_park=park,
            get_left_frame=lambda: frame("left"),
            get_right_frame=lambda: frame("right"),
            wait_for_left_frame=waiter("left"),
            wait_for_right_frame=waiter("right"),
            settings=_SETTINGS,
            camera_geometry=lambda: list(cameras.values()),
            runner=MountTestMoveRunner(),
        )
        panel._terrestrial_button.click()
        panel._connect_button.setChecked(True)
        panel._run_calibration_button.click()

        deadline = time.monotonic() + 120.0  # real-time safety bound only
        while True:
            while panel._runner.is_busy:
                assert time.monotonic() < deadline, "the move never completed"
                time.sleep(0.005)
            panel._poll()
            if not panel._calibration_queue and panel._pending is None:
                break

        assert panel.calibration_for("left") is not None, panel._result_label.text()
        assert panel.calibration_for("right") is not None, panel._result_label.text()
        assert client.parked is False  # the runner unparked the simulated mount
        moved = sum(abs(offset) for _axis, offset in client.axis_move_calls)
        assert clock.monotonic() == pytest.approx(moved / 2.0)  # every move took fake time
        assert math.hypot(client.ha_deg - ha0, client.dec_deg - dec0) * 3600.0 < 6.0
        assert client.monitor.interleavings == []
        panel.stop()
