"""S6.0 (#46/#31, audit P01/P02): Mount Align against the PRODUCTION OnStep adapters.

Mount Align panel -> `MountTestMoveRunner` -> the real `OnStepMountParkAdapter` /
`OnStepMountPulseAdapter` -> `OnStepConnection` -> `FakeOnStepIndiClient` (OnStepAdapter >= 0.4
over INDI: no timed pulse, only a finite degree-target axis move of 30"..10 deg). Camera frames
are a textured scene shifted by what the fake controller REALLY moved (its hour angle /
declination), optionally rotated and sign-flipped per rig, so the assertions can only pass if
the mount actually received the right moves.

The defect these pin: every bootstrap/nudge/screen move used to go through `pulse_axis`, which
this adapter always refuses, so on the real rig the mount never moved at all. The review
follow-ups pin the adapter's 30" floor: no commanded move may fall below it, and a screen move
is validated as a whole before anything is sent.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

import numpy as np
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, MountAxis
from astrotool_core.mount.movement_sizing import CameraGeometry
from astrotool_core.onstep import MIN_AXIS_ARCSEC, OnStepMountParkAdapter, OnStepMountPulseAdapter
from astrotool_core.testing.fake_onstep_indi_client import (
    FakeOnStepIndiClient,
    make_fake_onstep_indi_connection,
)
from collimation_tool.ui.mount_test_move_panel import (
    MountTestMovePanel,
    MovementSize,
    ScreenDirection,
)
from collimation_tool.ui.mount_test_move_runner import MountTestMoveRunner

_SETTINGS = MountAlignmentSettings(
    settle_ms=0,
    frame_settle_ms=0,
    stability_sample_interval_s=0.0,
    stability_timeout_s=3.0,
)
_LEFT = CameraGeometry("left", 200, 120, 3.0)  # FOV 600" x 360"
_RIGHT = CameraGeometry("right", 300, 100, 12.0)  # FOV 3600" x 1200"
_SCALE = {"left": 3.0, "right": 12.0}


def _main(fov_width_arcsec: float) -> CameraGeometry:
    """A 16:9 main camera of the given FOV width (scaled-down pixel count for speed)."""
    return CameraGeometry("left", 240, 135, fov_width_arcsec / 240)


#: GPCMOS02000 / 180 mm guide at the size the S6.0 review used (1595" x 897").
_GUIDE = CameraGeometry("right", 240, 135, 1595.0 / 240)


def _texture(seed: int, shape: tuple[int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random(shape).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0, dtype=np.float32)


class _Rig:
    """Production adapters over one fake OnStep INDI client + a sky that follows it.

    `rot_deg` rotates the camera against the mount axes; `ra_sign`/`dec_sign` flip an axis'
    image direction (an arbitrarily mounted camera)."""

    def __init__(
        self,
        cameras: tuple[CameraGeometry, CameraGeometry] = (_LEFT, _RIGHT),
        *,
        rot_deg: float = 0.0,
        ra_sign: float = 1.0,
        dec_sign: float = 1.0,
    ) -> None:
        connection, made = make_fake_onstep_indi_connection()
        self.park = OnStepMountParkAdapter(connection)
        self.mount = OnStepMountPulseAdapter(connection)
        self.park.connect()
        self.mount.connect()
        self.client: FakeOnStepIndiClient = made[0]
        self.ha0, self.dec0 = self.client.ha_deg, self.client.dec_deg
        self.cameras = {camera.key: camera for camera in cameras}
        self.scale = {key: camera.arcsec_per_px or 1.0 for key, camera in self.cameras.items()}
        self.base = {
            key: _texture(seed, (camera.height_px, camera.width_px))
            for seed, (key, camera) in enumerate(self.cameras.items(), start=1)
        }
        self.rot = math.radians(rot_deg)
        self.ra_sign, self.dec_sign = ra_sign, dec_sign

    def moved_arcsec(self) -> tuple[float, float]:
        return (
            (self.client.ha_deg - self.ha0) * 3600.0,
            (self.client.dec_deg - self.dec0) * 3600.0,
        )

    def image_shift_px(self, key: str) -> tuple[float, float]:
        """Where the scene has moved in this camera (x right, y down), in pixels."""
        ra, dec = self.moved_arcsec()
        ra, dec = ra * self.ra_sign, dec * self.dec_sign
        x = ra * math.cos(self.rot) - dec * math.sin(self.rot)
        y = ra * math.sin(self.rot) + dec * math.cos(self.rot)
        return x / self.scale[key], y / self.scale[key]

    def frame(self, key: str) -> np.ndarray:
        dx, dy = self.image_shift_px(key)
        return np.roll(np.roll(self.base[key], round(dy), axis=0), round(dx), axis=1)

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

    def sizes_arcsec(self) -> list[float]:
        return [abs(offset) * 3600.0 for _axis, offset in self.client.axis_move_calls]

    def panel(self) -> MountTestMovePanel:
        cameras = list(self.cameras.values())
        panel = MountTestMovePanel(
            self.mount,
            mount_park=self.park,
            get_left_frame=self.getter("left"),
            get_right_frame=self.getter("right"),
            wait_for_left_frame=self.waiter("left"),
            wait_for_right_frame=self.waiter("right"),
            settings=_SETTINGS,
            camera_geometry=lambda: cameras,
        )
        panel._terrestrial_button.click()  # texture-based (cross-correlation) measurement
        panel._connect_button.setChecked(True)
        return panel


def _drain(panel: MountTestMovePanel, *, timeout_s: float = 120.0) -> None:
    """Run the panel's poll loop until no calibration/move is queued or in flight."""
    deadline = time.monotonic() + timeout_s
    while True:
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "the move never completed"
            time.sleep(0.005)
        panel._poll()
        if not panel._calibration_queue and panel._pending is None and not panel._runner.is_busy:
            return
        assert time.monotonic() < deadline, "the sequence never completed"


def _calibrated(rig: _Rig) -> MountTestMovePanel:
    panel = rig.panel()
    panel._run_calibration_button.click()
    _drain(panel)
    assert panel.calibration_for("left") is not None, panel._result_label.text()
    return panel


def _only_move(rig: _Rig) -> tuple[str, float]:
    """The single axis move commanded so far, as (axis, signed arcsec)."""
    assert len(rig.client.axis_move_calls) == 1, rig.client.axis_move_calls
    axis, offset_deg = rig.client.axis_move_calls[0]
    return axis, offset_deg * 3600.0


@pytest.fixture
def rig() -> _Rig:
    return _Rig()


class TestCalibrationMovesTheRealAdapter:
    def test_calibration_moves_the_mount_and_produces_a_matrix(
        self, qapp: object, rig: _Rig
    ) -> None:
        panel = rig.panel()
        panel._run_calibration_button.click()
        _drain(panel)

        assert rig.client.axis_move_calls, (
            f"the mount never received a move: {panel._result_label.text()}"
        )
        assert "no timed pulse primitive" not in panel._result_label.text()
        assert panel.calibration_for("left") is not None, panel._result_label.text()
        panel.stop()

    def test_the_first_move_is_the_up_front_25_percent_angular_seed(
        self, qapp: object, rig: _Rig
    ) -> None:
        """AGENTS.md "Mount Align movement policy": ~25% of the smallest participating FOV
        (600" wide -> 150"), commanded angularly -- no timed bootstrap."""
        panel = rig.panel()
        panel._run_calibration_button.click()
        _drain(panel)

        assert rig.client.axis_move_calls
        axis, offset_deg = rig.client.axis_move_calls[0]
        assert axis == "ra"
        assert offset_deg > 0
        assert abs(offset_deg * 3600.0 - 150.0) < 1.0
        panel.stop()

    def test_the_wide_camera_gets_its_own_larger_angular_follow_up(
        self, qapp: object, rig: _Rig
    ) -> None:
        """#46 smallest-FOV-first: the 3600"-wide camera sees only ~4% of its frame from the
        150" seed, so it gets one larger angular move sized for ITS frame (~900")."""
        panel = rig.panel()
        panel._run_calibration_button.click()
        _drain(panel)

        assert any(700.0 <= size <= 1100.0 for size in rig.sizes_arcsec()), rig.sizes_arcsec()
        assert panel.calibration_for("right") is not None, panel._result_label.text()
        panel.stop()

    def test_a_calibrated_run_returns_the_mount_to_its_start(self, qapp: object, rig: _Rig) -> None:
        panel = rig.panel()
        panel._run_calibration_button.click()
        _drain(panel)

        assert len(rig.client.axis_move_calls) >= 8  # both axes really went out and back
        ra, dec = rig.moved_arcsec()
        assert abs(ra) < 6.0 and abs(dec) < 6.0, (ra, dec)
        panel.stop()

    def test_a_refusal_mid_calibration_leaves_the_panel_idle_and_retryable(
        self, qapp: object, rig: _Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_move = rig.client.move_axis_deg
        calls = {"n": 0}

        def refuse_third(axis: str, offset_deg: float, **kwargs: float) -> object:
            calls["n"] += 1
            if calls["n"] == 3:
                raise RuntimeError("axis_motion_reached_hard_limit")
            return real_move(axis, offset_deg, **kwargs)

        monkeypatch.setattr(rig.client, "move_axis_deg", refuse_third)
        panel = rig.panel()
        panel._run_calibration_button.click()
        _drain(panel)

        assert "axis_motion_reached_hard_limit" in panel._result_label.text()
        assert panel._calibration_queue == [] and panel._pending is None
        assert not panel._runner.is_busy
        assert panel._run_calibration_button.isEnabled()
        assert panel.calibration_for("left") is None

        monkeypatch.setattr(rig.client, "move_axis_deg", real_move)
        panel._run_calibration_button.click()
        _drain(panel)
        assert panel.calibration_for("left") is not None, panel._result_label.text()
        panel.stop()


#: (main FOV width, camera rotation deg, RA image sign, Dec image sign). 195"/283" are the S6.0
#: review's literal (harsh) main widths -- their 25% Dec probe falls below the 30" floor --
#: 780" the real G3M678M/C8 width (195" is its 25% seed).
_REALISTIC = [
    (195.0, 0.0, 1.0, 1.0),
    (195.0, 10.0, 1.0, 1.0),
    (195.0, 0.0, 1.0, -1.0),
    (195.0, 35.0, -1.0, 1.0),
    (283.0, 10.0, 1.0, 1.0),
    (283.0, 0.0, -1.0, -1.0),
    (780.0, 10.0, 1.0, 1.0),
]


class TestRealisticOpticsRespectTheMountsSmallestMove:
    @pytest.mark.parametrize(("main_width", "rot", "ra_sign", "dec_sign"), _REALISTIC)
    def test_calibration_succeeds_and_never_commands_below_the_floor(
        self, qapp: object, main_width: float, rot: float, ra_sign: float, dec_sign: float
    ) -> None:
        rig = _Rig((_main(main_width), _GUIDE), rot_deg=rot, ra_sign=ra_sign, dec_sign=dec_sign)
        panel = _calibrated(rig)

        sizes = rig.sizes_arcsec()
        assert sizes and min(sizes) >= MIN_AXIS_ARCSEC, sizes
        assert "outside OnStepAdapter's supported axis-move range" not in (
            panel._result_label.text()
        )
        ra, dec = rig.moved_arcsec()
        # back at the start, up to at most one sub-floor residual that cannot be commanded
        assert abs(ra) < MIN_AXIS_ARCSEC and abs(dec) < MIN_AXIS_ARCSEC, (ra, dec)
        panel.stop()


class TestManualMovesMoveTheRealAdapter:
    def test_an_uncalibrated_ra_nudge_moves_the_mount(self, qapp: object, rig: _Rig) -> None:
        """AGENTS.md "Manual movement versus measurement": RA+ works before any
        CalibrationMatrix exists -- sized from the clicked camera's optics (half its frame)."""
        panel = rig.panel()
        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
        _drain(panel)

        axis, arcsec = _only_move(rig)
        assert axis == "ra"
        assert arcsec == pytest.approx(300.0)
        assert "Move failed" not in panel._result_label.text()
        panel.stop()

    def test_an_uncalibrated_dec_minus_nudge_moves_the_mount(self, qapp: object, rig: _Rig) -> None:
        panel = rig.panel()
        panel._on_nudge_clicked("right", MountAxis.AXIS2, AxisDirection.NEGATIVE)
        _drain(panel)

        axis, arcsec = _only_move(rig)
        assert axis == "dec"
        assert arcsec == pytest.approx(-1800.0)
        panel.stop()

    def test_a_calibrated_negative_nudge_moves_back_by_half_the_frame(self, qapp: object) -> None:
        rig = _Rig((_main(283.0), _GUIDE), rot_deg=10.0)
        panel = _calibrated(rig)
        before = len(rig.client.axis_move_calls)
        x0, _y0 = rig.image_shift_px("left")

        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.NEGATIVE)
        _drain(panel)

        new = rig.client.axis_move_calls[before:]
        assert len(new) == 1 and new[0][0] == "ra" and new[0][1] < 0, new
        x1, _y1 = rig.image_shift_px("left")
        # nudge_target_fraction (0.5) of the 240 px frame, measured on the rotated camera
        assert abs(abs(x1 - x0) - 0.5 * 240) < 0.15 * 240, (x0, x1)
        assert "Move failed" not in panel._result_label.text()
        panel.stop()

    def test_a_screen_move_after_calibration_moves_the_mount(self, qapp: object) -> None:
        """Large RIGHT on a 10-degree-rotated camera: the image goes RIGHT by ~30% of the
        frame and barely vertically -- direction AND magnitude, not just "something moved"."""
        rig = _Rig((_main(283.0), _GUIDE), rot_deg=10.0)
        panel = _calibrated(rig)
        x0, y0 = rig.image_shift_px("left")

        panel._size_buttons[MovementSize.LARGE].setChecked(True)
        panel._on_screen_move_clicked("left", ScreenDirection.RIGHT)
        _drain(panel)

        x1, y1 = rig.image_shift_px("left")
        assert "Move failed" not in panel._result_label.text(), panel._result_label.text()
        assert panel._last_error is None
        assert abs((x1 - x0) - 0.30 * 240) < 0.25 * 0.30 * 240, (x1 - x0, y1 - y0)
        assert abs(y1 - y0) < 0.25 * 0.30 * 240, (x1 - x0, y1 - y0)
        panel.stop()

    def test_a_medium_screen_move_skips_a_sub_floor_component_within_tolerance(
        self, qapp: object
    ) -> None:
        """Medium RIGHT (15% of 283" = 42") on a 10-degree camera needs a ~7" Dec component the
        mount cannot make; dropping it leaves a small off-axis error, so the move still runs."""
        rig = _Rig((_main(283.0), _GUIDE), rot_deg=10.0)
        panel = _calibrated(rig)
        before = len(rig.client.axis_move_calls)
        x0, _y0 = rig.image_shift_px("left")

        panel._size_buttons[MovementSize.MEDIUM].setChecked(True)
        panel._on_screen_move_clicked("left", ScreenDirection.RIGHT)
        _drain(panel)

        new = rig.client.axis_move_calls[before:]
        assert [axis for axis, _offset in new] == ["ra"], new
        assert all(abs(offset) * 3600.0 >= MIN_AXIS_ARCSEC for _axis, offset in new)
        x1, _y1 = rig.image_shift_px("left")
        assert abs((x1 - x0) - 0.15 * 240) < 0.25 * 0.15 * 240, x1 - x0
        assert "Move failed" not in panel._result_label.text()
        panel.stop()

    @pytest.mark.parametrize(
        ("size", "direction"),
        [(MovementSize.SMALL, ScreenDirection.RIGHT), (MovementSize.MEDIUM, ScreenDirection.UP)],
    )
    def test_a_screen_move_below_the_floor_is_refused_before_any_motion(
        self, qapp: object, size: MovementSize, direction: ScreenDirection
    ) -> None:
        """Small RIGHT (5% of 195" = 10") / Medium UP (15% of 110" = 16") cannot be made by a
        mount whose smallest move is 30": refused up front, nothing sent, no partial move."""
        rig = _Rig((_main(195.0), _GUIDE), rot_deg=10.0)
        panel = _calibrated(rig)
        before = len(rig.client.axis_move_calls)
        position = rig.moved_arcsec()

        panel._size_buttons[size].setChecked(True)
        panel._on_screen_move_clicked("left", direction)
        _drain(panel)

        assert len(rig.client.axis_move_calls) == before
        assert rig.moved_arcsec() == position
        assert panel._last_error is not None and "nothing was sent" in panel._last_error
        assert not panel._runner.is_busy
        panel.stop()


class TestRunnerAgainstTheRealAdapter:
    def test_an_angular_refusal_is_reported_not_masked_by_a_timed_fallback(
        self, rig: _Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P02: on a mount without timed pulses, an at-home refusal must surface as-is --
        the timed fallback is known-unsupported and only replaced the real reason."""

        def refuse(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("axis_motion_refused_at_home")

        monkeypatch.setattr(rig.client, "move_axis_deg", refuse)
        runner = MountTestMoveRunner()
        assert runner.submit_sequence(
            rig.park,
            rig.mount,
            [(MountAxis.AXIS1, AxisDirection.POSITIVE, 1000, 150.0)],
            park_after=False,
        )
        while runner.is_busy:
            time.sleep(0.005)
        outcome = runner.take_latest()

        assert outcome is not None and not outcome.pulsed
        assert outcome.error is not None
        assert "axis_motion_refused_at_home" in outcome.error
        assert "no timed pulse primitive" not in outcome.error
        assert outcome.motion_paths == ("angular",)

    def test_a_duration_only_step_is_refused_explicitly_without_retrying_pulse_axis(
        self, rig: _Rig
    ) -> None:
        """A timed step on a mount that reports no timed pulses is refused once, by the
        runner, naming the missing angular size -- never retried against `pulse_axis`."""
        runner = MountTestMoveRunner()
        started = time.monotonic()
        assert runner.submit(
            rig.park, rig.mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 1000, park_after=False
        )
        while runner.is_busy:
            time.sleep(0.005)
        outcome = runner.take_latest()

        assert outcome is not None and not outcome.pulsed
        assert outcome.error is not None and "angular size" in outcome.error
        assert time.monotonic() - started < 1.5  # not 6 x 0.3 s of doomed pulse retries
        assert rig.client.axis_move_calls == []
