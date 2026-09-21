"""Issues #31/#46 + AGENTS.md on an angular-capable mount (OnStepAdapter >= 0.3.5).

The first move per direction is a TIMED bootstrap at the controller's centering rate; the
rate MEASURED from the image shift (x the camera's plate scale) is installed into the mount
at runtime; every later move of that direction is an ANGULAR move of the calculated
~25%-of-frame size. Proven here without hardware, against `FakeAngularMountAdapter`, whose
unknown TRUE rates differ from the seed -- so a correct outcome can only come from measuring.
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
from astrotool_core.testing.fake_mount import FakeAngularMountAdapter, FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel

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
    panel = MountTestMovePanel(
        rig.mount,
        mount_park=FakeMountPark(start_parked=True),
        get_left_frame=rig.getter("left"),
        get_right_frame=rig.getter("right"),
        wait_for_left_frame=rig.waiter("left"),
        wait_for_right_frame=rig.waiter("right"),
        settings=_SETTINGS,
        camera_geometry=lambda: [_LEFT, _RIGHT],
    )
    panel._terrestrial_button.click()  # texture-based (cross-correlation) measurement
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


def _true_rates(scale: float = 1.0) -> dict[tuple[MountAxis, AxisDirection], float]:
    # Deliberately NOT the seed (8x sidereal = 120.3 "/s) and different per direction.
    base = {
        (MountAxis.AXIS1, AxisDirection.POSITIVE): 150.0,
        (MountAxis.AXIS1, AxisDirection.NEGATIVE): 142.0,
        (MountAxis.AXIS2, AxisDirection.POSITIVE): 96.0,
        (MountAxis.AXIS2, AxisDirection.NEGATIVE): 90.0,
    }
    return {k: v * scale for k, v in base.items()}


def _events(panel: MountTestMovePanel, name: str) -> list[dict[str, object]]:
    return [e for e in panel._sizing_log if e.get("event") == name]


class TestBootstrapThenAngular:
    def _calibrated(self) -> tuple[_SkyRig, MountTestMovePanel]:
        rig = _SkyRig(FakeAngularMountAdapter(true_rate=_true_rates()))
        panel = _panel(rig)
        _run(panel)
        return rig, panel

    def test_the_very_first_move_is_a_timed_bootstrap_at_the_center_rate(
        self, qapp: object
    ) -> None:
        rig, panel = self._calibrated()
        assert rig.mount.pulse_log, "a bootstrap move was expected"
        assert rig.mount.rate_log[0] is None  # no preset -> OnStepAdapter's centering rate
        first = rig.mount.pulse_log[0]
        assert first[0] is MountAxis.AXIS1 and first[1] is AxisDirection.POSITIVE
        # sized from optics: 25% of the narrow 600" frame = 150" at the 8x seed (~1.25 s)
        assert abs(first[2] - 1246) <= 15
        panel.stop()

    def test_every_direction_gets_its_measured_rate_installed_close_to_the_truth(
        self, qapp: object
    ) -> None:
        rig, panel = self._calibrated()
        truth = _true_rates()
        for axis in _AXES:
            for direction in _DIRECTIONS:
                installed = rig.mount.installed_rate(axis, direction)
                assert installed is not None, f"no rate for {axis.name} {direction.name}"
                assert abs(installed - truth[(axis, direction)]) / truth[(axis, direction)] < 0.04
        assert len(_events(panel, "rate_installed")) >= 4
        panel.stop()

    def test_after_the_bootstrap_moves_are_angular_not_timed(self, qapp: object) -> None:
        rig, panel = self._calibrated()
        assert rig.mount.angular_log, "no angular move was made"
        # Exactly one timed bootstrap (per attempt) before a direction's rate is known; every
        # later move of that direction is angular.
        motions = _events(panel, "motion")
        by_direction: dict[tuple[object, object], list[object]] = {}
        for event in motions:
            by_direction.setdefault((event["axis"], event["direction"]), []).append(event["path"])
        for paths in by_direction.values():
            first_angular = next((i for i, p in enumerate(paths) if p == "angular"), None)
            assert first_angular is not None and first_angular >= 1
            assert all(p == "angular" for p in paths[first_angular:]), paths
            assert all(p == "timed" for p in paths[:first_angular]), paths
        panel.stop()

    def test_the_angular_size_is_the_calculated_25_percent_of_the_frame(self, qapp: object) -> None:
        rig, panel = self._calibrated()
        sizes = [arcsec for _a, _d, arcsec in rig.mount.angular_log]
        # 25% of the narrow camera's 600" width = 150" (band 120..180"); the wide camera's own
        # follow-up is ~25% of its 3600" width.
        assert any(120.0 <= size <= 180.0 for size in sizes), sizes
        panel.stop()

    def test_the_result_lands_in_the_25_percent_band_and_the_matrix_is_measured(
        self, qapp: object
    ) -> None:
        _rig, panel = self._calibrated()
        matrix = panel.calibration_for("left")
        assert matrix is not None
        for axis, frame_dim in ((MountAxis.AXIS1, 200), (MountAxis.AXIS2, 120)):
            response = matrix.response_for(axis, AxisDirection.POSITIVE)
            shift = max(abs(response.dx_px), abs(response.dy_px))
            assert 0.20 <= shift / frame_dim <= 0.30 or axis is MountAxis.AXIS2, (axis, shift)
        panel.stop()

    def test_the_mount_is_returned_to_where_it_started(self, qapp: object) -> None:
        rig, panel = self._calibrated()
        for axis in _AXES:
            assert abs(rig.mount.sky_arcsec[axis]) < 12.0  # within a few pixels of the start
        panel.stop()

    def test_a_mount_without_the_capability_still_calibrates_timed_only(self, qapp: object) -> None:
        """Legacy path (FakeMountAdapter): unchanged behaviour, no angular fields."""
        legacy = FakeMountAdapter()
        assert not hasattr(legacy, "move_angular")
        frame = _texture(1, (120, 200))
        panel = MountTestMovePanel(
            legacy,
            mount_park=FakeMountPark(start_parked=True),
            get_left_frame=lambda: frame,
            get_right_frame=lambda: frame,
            settings=_SETTINGS,
            camera_geometry=lambda: [_LEFT, _RIGHT],
        )
        panel._init_sizing()
        assert panel._angular_active is False
        assert panel._calibration_rate_preset == "6"  # the legacy preset is untouched
        panel.stop()


class TestAtHomeRefusalFallsBackToTimed:
    def test_calibration_still_completes_and_the_fallback_is_recorded(self, qapp: object) -> None:
        rig = _SkyRig(
            FakeAngularMountAdapter(
                true_rate=_true_rates(), refuse_angular="refused: axis_motion_refused_at_home"
            )
        )
        panel = _panel(rig)
        _run(panel)

        assert rig.mount.angular_log == []  # never accepted
        paths = [e["path"] for e in _events(panel, "motion")]
        assert "timed_fallback" in paths
        assert panel.calibration_for("left") is not None
        for axis in _AXES:
            assert abs(rig.mount.sky_arcsec[axis]) < 12.0
        panel.stop()


class TestOtherRefusalsStayRefusals:
    def test_a_safety_refusal_aborts_the_calibration_without_a_timed_workaround(
        self, qapp: object
    ) -> None:
        rig = _SkyRig(
            FakeAngularMountAdapter(
                true_rate=_true_rates(), refuse_angular="refused: axis_motion_reached_hard_limit"
            )
        )
        panel = _panel(rig)

        _run(panel)
        while panel._runner.is_busy:  # the stranded-return pulse of the abort
            time.sleep(0.005)
        panel._poll()

        # The bootstrap timed move(s) happened; once the adapter refused an angular move for a
        # safety reason nothing timed was used to get around it, and the UI is recoverable.
        assert panel._calibration_queue == []
        assert panel._pending is None
        assert not panel._runner.is_busy
        assert "hard_limit" in panel._result_label.text()
        assert rig.mount.angular_log == []
        for axis in _AXES:  # the abort returned the mount from where the bootstrap left it
            assert abs(rig.mount.sky_arcsec[axis]) < 15.0
        panel.stop()
