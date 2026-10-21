"""Issues #31/#46 + AGENTS.md on the production mount contract (S6.8d, R9).

Every calibration move is an ANGULAR move of the calculated ~25%-of-frame size, sized up front
from the optics -- no timed bootstrap, no installed rate (both deleted in S6.8d with the timed
path; before, the first move per direction was a timed bootstrap whose measured rate was
installed into the mount). Proven against `FakeAngularMountAdapter`, which moves the sky by a
different amount than commanded per direction -- so a result in band can only come from the
MEASURED displacement.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, MountAxis
from astrotool_core.mount.movement_sizing import CameraGeometry
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer
from astrotool_core.testing.fake_mount import FakeAngularMountAdapter, FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.timing import FakeClock
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel
from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner

_SETTINGS = MountAlignmentSettings(
    settle_ms=0,
    frame_settle_ms=0,
    stability_sample_interval_s=0.0,
    stability_timeout_s=3.0,
)
_LEFT = CameraGeometry("left", 200, 120, 3.0)  # FOV 600" x 360"
_RIGHT = CameraGeometry("right", 300, 100, 12.0)  # FOV 3600" x 1200"
_TRUE_SCALE = {"left": 3.0, "right": 12.0}  # real arcsec/px (matches the configured optics)
_AXES = (MountAxis.AXIS1, MountAxis.AXIS2)
_DIRECTIONS = (AxisDirection.POSITIVE, AxisDirection.NEGATIVE)


def _texture(seed: int, shape: tuple[int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random(shape).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0, dtype=np.float32)


class _SkyRig:
    """Every frame is the scene shifted by the mount's cumulative REAL sky motion."""

    def __init__(self, mount: FakeAngularMountAdapter) -> None:
        self.mount = mount
        self.shapes = {"left": (120, 200), "right": (100, 300)}
        self.base = {
            "left": _texture(1, self.shapes["left"]),
            "right": _texture(2, self.shapes["right"]),
        }

    def frame(self, key: str) -> np.ndarray:
        dx = round(self.mount.sky_arcsec[MountAxis.AXIS1] / _TRUE_SCALE[key])
        dy = round(self.mount.sky_arcsec[MountAxis.AXIS2] / _TRUE_SCALE[key])
        return np.roll(np.roll(self.base[key], dy, axis=0), dx, axis=1)

    def getter(self, key: str) -> Callable[[], np.ndarray]:
        return lambda: self.frame(key)

    def waiter(self, key: str) -> Callable[[float, float], FrameAcquisitionResult]:
        def wait(_reference: float, _timeout: float) -> FrameAcquisitionResult:
            return FrameAcquisitionResult(
                status=FrameAcquisitionStatus.OK,
                frame=DeliveredFrame(
                    pixels=self.frame(key),
                    captured_at_monotonic=time.monotonic(),
                    exposure_seconds=0.01,
                ),
            )

        return wait


def _panel(rig: _SkyRig) -> MountTestMovePanel:
    rig.mount.connect()
    park = FakeMountPark(start_parked=True)
    panel = MountTestMovePanel(
        rig.mount,
        mount_park=park,
        tracking_enforcer=TrackingEnforcer(
            park, OperatingMode.TERRESTRIAL, settle_timeout_s=0, clock=FakeClock()
        ),  # #48: terrestrial measurement comes from the global mode's owner
        get_left_frame=rig.getter("left"),
        get_right_frame=rig.getter("right"),
        wait_for_left_frame=rig.waiter("left"),
        wait_for_right_frame=rig.waiter("right"),
        settings=_SETTINGS,
        camera_geometry=lambda: [_LEFT, _RIGHT],
        runner=MountTestMoveRunner(clock=FakeClock()),  # S3b: runner waits on fake time
    )
    panel._connect_button.setChecked(True)
    return panel


def _run(panel: MountTestMovePanel, *, timeout_s: float = 120.0) -> None:
    panel._run_calibration_button.click()
    deadline = time.monotonic() + timeout_s
    while panel._calibration_queue or panel._pending is not None:
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "calibration never completed"
            time.sleep(0.005)
        panel._poll()
        assert time.monotonic() < deadline, "calibration never completed"


def _scales() -> dict[tuple[MountAxis, AxisDirection], float]:
    """Sky arcsec moved per commanded arcsec -- deliberately not 1 and different per direction."""
    return {
        (MountAxis.AXIS1, AxisDirection.POSITIVE): 1.25,
        (MountAxis.AXIS1, AxisDirection.NEGATIVE): 1.18,
        (MountAxis.AXIS2, AxisDirection.POSITIVE): 0.80,
        (MountAxis.AXIS2, AxisDirection.NEGATIVE): 0.75,
    }


def _events(panel: MountTestMovePanel, name: str) -> list[dict[str, object]]:
    return [e for e in panel._sizing_log if e.get("event") == name]


class TestEveryMoveIsAngular:
    def _calibrated(self) -> tuple[_SkyRig, MountTestMovePanel]:
        rig = _SkyRig(FakeAngularMountAdapter(scale=_scales()))
        panel = _panel(rig)
        _run(panel)
        return rig, panel

    def test_the_very_first_move_is_the_up_front_25_percent_angle(self, qapp: object) -> None:
        rig, panel = self._calibrated()
        assert rig.mount.pulse_log == [], "no timed pulse may be sent (S6.8d)"
        axis, direction, arcsec = rig.mount.angular_log[0]
        assert (axis, direction) == (MountAxis.AXIS1, AxisDirection.POSITIVE)
        assert abs(arcsec - 150.0) < 1.0  # 25% of the narrow 600" frame
        panel.stop()

    def test_every_logged_motion_is_angular(self, qapp: object) -> None:
        _rig, panel = self._calibrated()
        paths = {e["path"] for e in _events(panel, "motion")}
        assert paths == {"angular"}, paths
        assert _events(panel, "rate_installed") == []
        panel.stop()

    def test_the_result_lands_in_the_25_percent_band_and_the_matrix_is_measured(
        self, qapp: object
    ) -> None:
        _rig, panel = self._calibrated()
        matrix = panel.calibration_for("left")
        assert matrix is not None
        response = matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
        shift = max(abs(response.dx_px), abs(response.dy_px))
        assert 0.20 <= shift / 200 <= 0.30, shift
        panel.stop()

    def test_the_mount_is_returned_to_where_it_started(self, qapp: object) -> None:
        """The return is sized from the COMMANDED angles, so it is exact for a mount whose
        mismatch is the same both ways (an asymmetric one leaves the asymmetry -- as on the
        real mount)."""
        rig = _SkyRig(FakeAngularMountAdapter(scale=1.2))
        panel = _panel(rig)
        _run(panel)
        for axis in _AXES:
            assert abs(rig.mount.sky_arcsec[axis]) < 20.0  # within a few pixels of the start
        panel.stop()

    def test_a_mount_without_angular_moves_is_never_pulsed(self, qapp: object) -> None:
        """S6.8d: the former timed-only calibration of `FakeMountAdapter` is gone -- such a
        mount gets no move at all (refused before anything is sent)."""
        legacy = FakeMountAdapter()
        legacy.connect()
        frame = _texture(1, (120, 200))
        park = FakeMountPark(start_parked=True)
        panel = MountTestMovePanel(
            legacy,
            mount_park=park,
            tracking_enforcer=TrackingEnforcer(
                park, OperatingMode.TERRESTRIAL, settle_timeout_s=0, clock=FakeClock()
            ),
            get_left_frame=lambda: frame,
            get_right_frame=lambda: frame,
            settings=_SETTINGS,
            camera_geometry=lambda: [_LEFT, _RIGHT],
            runner=MountTestMoveRunner(clock=FakeClock()),
        )
        panel._connect_button.setChecked(True)
        _run(panel)
        assert legacy.pulse_log == []
        assert panel.calibration_for("left") is None
        assert "no angular" in panel._result_label.text(), panel._result_label.text()
        panel.stop()


class TestRefusalsAreNeverWorkedAround:
    """S6.8d: an at-home refusal used to fall back to a timed move; now every refusal is final."""

    def test_an_at_home_refusal_aborts_the_calibration_with_its_reason(
        self, qapp: object
    ) -> None:
        rig = _SkyRig(
            FakeAngularMountAdapter(refuse_angular="refused: axis_motion_refused_at_home")
        )
        panel = _panel(rig)
        _run(panel)
        assert "axis_motion_refused_at_home" in panel._result_label.text()
        assert rig.mount.pulse_log == [] and rig.mount.angular_log == []
        assert panel.calibration_for("left") is None
        panel.stop()

    def test_a_safety_refusal_aborts_the_calibration_and_the_ui_is_recoverable(
        self, qapp: object
    ) -> None:
        rig = _SkyRig(
            FakeAngularMountAdapter(refuse_angular="refused: axis_motion_reached_hard_limit")
        )
        panel = _panel(rig)

        _run(panel)
        while panel._runner.is_busy:
            time.sleep(0.005)
        panel._poll()

        assert panel._calibration_queue == []
        assert panel._pending is None
        assert not panel._runner.is_busy
        assert "hard_limit" in panel._result_label.text()
        assert rig.mount.angular_log == [] and rig.mount.pulse_log == []
        panel.stop()
