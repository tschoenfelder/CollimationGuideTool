"""Issue #46 in the Mount Align panel: calibration moves are sized to ~25% of
the frame, adapt to the MEASURED displacement, give a much wider camera its own
larger follow-up move without discarding the narrow camera's result, and stay
symmetric (+/-) and bounded (3 s cap, mount returned to its start).

The rig is a physics-style fake: every camera frame is the same textured scene
rolled by (cumulative signed pulse duration x that camera's px/ms vector), so
ANY retry / follow-up sequence just works -- unlike the scripted
`_stepped_frame_pair` helpers, which fix the number of captures up front.

The mount runner's waits run on a FakeClock (#53, S3b): on the real clock its stop_tracking
gate delay alone cost ~0.3 s per calibration move.

S6.8d (R9): the mount is the production-shaped `FakeAngularMountAdapter` -- every move is an
angle (sized up front; the duration envelope is widened to the widest camera's band), so sizes
here read in arcsec. The rate-preset tests (a calibrated move run at the preset its matrix was
measured at, S6.0 finding 6) went with the presets."""

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
from astrotool_core.testing.fake_mount import FakeAngularMountAdapter
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

# Nominal optics used for SIZING (what the config says).
_LEFT = CameraGeometry("left", 200, 120, 3.0)  # FOV 600" x 360"   (narrow / "Main-like")
_RIGHT = CameraGeometry("right", 300, 100, 12.0)  # FOV 3600" x 1200" (wide  / "Guide-like")
#: The widest camera's band edge: the largest normal calibration move (30% of 3600").
_ENVELOPE_ARCSEC = 0.30 * 3600.0


def _texture(seed: int, shape: tuple[int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random(shape).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0, dtype=np.float32)


class _PhysicsRig:
    """`true_scale`: the REAL arcsec/px per camera (may differ from the sizing
    geometry -- focus/finite distance -- which is exactly why measured pixels
    are authoritative)."""

    def __init__(self, mount: FakeAngularMountAdapter, true_scale: dict[str, float]) -> None:
        self.mount = mount
        self.true_scale = true_scale
        self.shapes = {
            "left": (_LEFT.height_px, _LEFT.width_px),
            "right": (_RIGHT.height_px, _RIGHT.width_px),
        }
        self.base = {
            "left": _texture(1, self.shapes["left"]),
            "right": _texture(2, self.shapes["right"]),
        }

    def offset(self, key: str) -> tuple[int, int]:
        px_per_arcsec = 1.0 / self.true_scale[key]
        dx = dy = 0.0
        for axis, direction, arcsec in self.mount.angular_log:
            signed = arcsec * px_per_arcsec * (1 if direction is AxisDirection.POSITIVE else -1)
            if axis is MountAxis.AXIS1:
                dx += signed
            else:
                dy += signed
        return round(dy), round(dx)

    def frame(self, key: str) -> np.ndarray:
        dy, dx = self.offset(key)
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


def _panel(
    rig: _PhysicsRig, *, geometry: bool = True, settings: MountAlignmentSettings = _SETTINGS
) -> MountTestMovePanel:
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
        settings=settings,
        camera_geometry=(lambda: [_LEFT, _RIGHT]) if geometry else None,
        runner=MountTestMoveRunner(clock=FakeClock()),
    )
    panel._connect_button.setChecked(True)
    return panel


def _run(panel: MountTestMovePanel, *, timeout_s: float = 90.0) -> None:
    panel._run_calibration_button.click()
    deadline = time.monotonic() + timeout_s
    while panel._calibration_queue or panel._pending is not None:
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "calibration never completed"
            time.sleep(0.005)
        panel._poll()
        assert time.monotonic() < deadline, "calibration never completed"


def _signed_net(rig: _PhysicsRig, axis: MountAxis) -> float:
    return sum(
        d if direction is AxisDirection.POSITIVE else -d
        for a, direction, d in rig.mount.angular_log
        if a is axis
    )


def _moves(rig: _PhysicsRig, axis: MountAxis | None = None) -> list[float]:
    return [d for a, _dr, d in rig.mount.angular_log if axis is None or a is axis]


class TestSizedCalibration:
    def test_the_first_pulse_is_sized_from_the_smallest_fov_not_a_fixed_pulse(
        self, qapp: object
    ) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        # 25% of the narrow camera's 600" width = 150"
        first = rig.mount.angular_log[0]
        assert first[0] is MountAxis.AXIS1
        assert abs(first[2] - 150.0) < 1.0
        assert rig.mount.pulse_log == []  # never a timed pulse
        panel.stop()

    def test_both_directions_of_an_axis_use_the_same_nominal_size(self, qapp: object) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        for axis in (MountAxis.AXIS1, MountAxis.AXIS2):
            log = rig.mount.angular_log
            positive = [d for a, dr, d in log if a is axis and dr is AxisDirection.POSITIVE]
            negative = [d for a, dr, d in log if a is axis and dr is AxisDirection.NEGATIVE]
            # Same nominal magnitudes in both directions (only the mirror image).
            assert sorted(positive) == sorted(negative)
        panel.stop()

    def test_the_narrow_camera_calibrates_in_the_25_percent_band(self, qapp: object) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        matrix = panel.calibration_for("left")
        assert matrix is not None
        response = matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
        assert 0.20 <= abs(response.dx_px) / 200 <= 0.30
        # 50 px per 150" -> per equivalent ms of the 8x unit (0.120328 "/ms)
        assert abs(response.px_per_ms - 0.120328 / 3.0) < 0.005
        panel.stop()

    def test_the_wide_camera_gets_a_larger_followup_and_the_narrow_result_survives(
        self, qapp: object
    ) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        sizes = _moves(rig)
        assert max(sizes) > 750.0  # the follow-up, ~25% of the wide frame (900")
        assert max(sizes) <= _ENVELOPE_ARCSEC + 1e-6  # never beyond the envelope
        wide = panel.calibration_for("right")
        narrow = panel.calibration_for("left")
        assert wide is not None and narrow is not None
        wide_response = wide.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
        narrow_response = narrow.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
        assert abs(wide_response.dx_px) / 300 >= 0.10  # measurable
        assert narrow_response.duration_ms < 2000  # the small (150") move's result was kept
        panel.stop()

    def test_the_mount_is_returned_to_its_start_after_a_clean_run(self, qapp: object) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        assert abs(_signed_net(rig, MountAxis.AXIS1)) < 1e-6
        assert abs(_signed_net(rig, MountAxis.AXIS2)) < 1e-6
        panel.stop()

    def test_no_pulse_ever_exceeds_the_normal_cap(self, qapp: object) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        assert all(d <= _ENVELOPE_ARCSEC + 1e-6 for d in _moves(rig))
        panel.stop()


class TestAdaptiveResizing:
    def test_an_undershoot_is_increased_automatically(self, qapp: object) -> None:
        # Reality moves the image 4x less than the configured optics predict.
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 12.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        first_axis1 = _moves(rig, MountAxis.AXIS1)
        assert abs(first_axis1[0] - 150.0) < 1.0
        assert first_axis1[1] > 1.8 * first_axis1[0]  # grown after the undershoot
        matrix = panel.calibration_for("left")
        assert matrix is not None
        fraction = abs(matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE).dx_px) / 200
        assert 0.20 <= fraction <= 0.30
        panel.stop()

    def test_an_overshoot_is_reduced_automatically(self, qapp: object) -> None:
        # Reality moves the image 1.5x more than predicted (37.5% of the width).
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 2.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        first_axis1 = _moves(rig, MountAxis.AXIS1)
        assert first_axis1[1] < 0.8 * first_axis1[0]
        matrix = panel.calibration_for("left")
        assert matrix is not None
        fraction = abs(matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE).dx_px) / 200
        assert 0.20 <= fraction <= 0.30
        panel.stop()

    def test_discarded_probe_moves_do_not_leave_the_mount_displaced(self, qapp: object) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 12.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        assert abs(_signed_net(rig, MountAxis.AXIS1)) < 1e-6
        assert abs(_signed_net(rig, MountAxis.AXIS2)) < 1e-6
        panel.stop()

    def test_the_second_axis_starts_from_the_first_axis_accepted_size(self, qapp: object) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 12.0, "right": 12.0})
        panel = _panel(rig)

        _run(panel)

        axis1 = _moves(rig, MountAxis.AXIS1)
        axis2 = _moves(rig, MountAxis.AXIS2)
        accepted_axis1 = axis1[1]  # first retry after the undershoot
        assert abs(axis2[0] - accepted_axis1) <= 1.0  # no repeat of the undershoot
        panel.stop()


class TestBoundedOutcomes:
    def test_a_camera_that_stays_unmeasurable_even_at_the_cap_is_reported_not_faked(
        self, qapp: object
    ) -> None:
        # The wide camera moves 5x less than its configured optics predict: even the largest
        # normal move (the 1080" envelope) reaches only ~6% of its frame (<10%).
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 60.0})
        panel = _panel(rig)

        _run(panel)

        assert panel.calibration_for("left") is not None  # narrow camera unaffected
        assert panel.calibration_for("right") is None
        assert all(d <= _ENVELOPE_ARCSEC + 1e-6 for d in _moves(rig))  # no silent extension
        assert "envelope" in panel._result_label.text().lower()
        panel.stop()


class TestWithoutOptics:
    def test_without_optics_the_fixed_size_is_used_as_an_angle(
        self, qapp: object
    ) -> None:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 12.0})
        panel = _panel(rig, geometry=False)

        _run(panel)

        assert {round(d, 6) for d in _moves(rig)} == {round(1000 * 0.120328, 6)}  # 1000 ms
        panel.stop()


class TestCalibratedMovesMoveTheRequestedFraction:
    """A move solved from the matrix moves what it was asked to (de295656's over-move class;
    S6.8d: there is no rate preset any more to get wrong)."""

    def _calibrated(self) -> tuple[_PhysicsRig, MountTestMovePanel]:
        rig = _PhysicsRig(FakeAngularMountAdapter(), {"left": 3.0, "right": 12.0})
        panel = _panel(rig)
        _run(panel)
        assert panel.calibration_for("left") is not None
        return rig, panel

    def test_a_calibrated_nudge_moves_the_requested_fraction_of_the_frame(
        self, qapp: object
    ) -> None:
        rig, panel = self._calibrated()
        before = rig.offset("left")[1]
        logged = len(rig.mount.angular_log)

        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
        deadline = time.monotonic() + 30.0
        while len(rig.mount.angular_log) == logged or panel._runner.is_busy or panel._pending:
            assert time.monotonic() < deadline, "the nudge never ran"
            time.sleep(0.005)
            panel._poll()

        moved = rig.offset("left")[1] - before
        target = _SETTINGS.nudge_target_fraction * _LEFT.width_px  # 100 px
        assert abs(moved - target) <= 0.1 * target, moved
        panel.stop()

    def test_a_screen_move_is_an_angular_move(self, qapp: object) -> None:
        rig, panel = self._calibrated()
        logged = len(rig.mount.angular_log)
        from collimation_tool.ui.mount_test_move_panel import ScreenDirection

        panel._on_screen_move_clicked("left", ScreenDirection.RIGHT)
        deadline = time.monotonic() + 30.0
        while panel._runner.is_busy or panel._pending is not None:
            assert time.monotonic() < deadline, "the screen move never ran"
            time.sleep(0.005)
            panel._poll()

        assert len(rig.mount.angular_log) > logged
        assert rig.mount.pulse_log == []
        panel.stop()
