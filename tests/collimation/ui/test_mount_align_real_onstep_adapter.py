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
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

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
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer
from astrotool_core.mount.tracking_mode import MOUNT_BUSY_REASON, TrackingMode
from astrotool_core.onstep import (
    MIN_AXIS_ARCSEC,
    OnStepFocuserAdapter,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
)
from astrotool_core.testing import (
    ObservableRLock,
    OnStepScenario,
    install_observable_operation_lock,
    make_simulated_onstep_connection,
)
from astrotool_core.testing.fake_onstep_indi_client import (
    FakeOnStepIndiClient,
    make_fake_onstep_indi_connection,
)
from astrotool_core.testing.sim_onstep import SimulatedOnStepIndiClient
from astrotool_core.timing import FakeClock
from collimation_tool.ui.focuser_panel import FocuserPanel
from collimation_tool.ui.mount_park_panel import MountParkPanel
from collimation_tool.ui.mount_test_move_panel import (
    MountTestMovePanel,
    MovementSize,
    ScreenDirection,
    _CaptureJob,
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
        clock: FakeClock | None = None,
    ) -> None:
        # S6.0c: with a clock, the #51 simulator (a GOTO takes |offset| / rate of that clock)
        connection, made = (
            make_simulated_onstep_connection(OnStepScenario(axis_rate_deg_per_s=0.1), clock=clock)
            if clock is not None
            else make_fake_onstep_indi_connection()
        )
        self.park = OnStepMountParkAdapter(connection)
        self.mount = OnStepMountPulseAdapter(connection)
        #: S3b (#53): on the instant fake connection the runner's own waits (stop_tracking gate
        #: delay, settle) run on a private FakeClock -- they cost ~0.3 s real time per move and
        #: are not what these tests observe. With the simulator clock (S6.0c's cross-thread
        #: Stop tests) the runner keeps the real clock, as before.
        self.runner_clock: FakeClock | None = FakeClock() if clock is None else None
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

    def panel(self, *, tracking_enforcer: TrackingEnforcer | None = None) -> MountTestMovePanel:
        cameras = list(self.cameras.values())
        panel = MountTestMovePanel(
            self.mount,
            mount_park=self.park,
            # #48: terrestrial measurement comes from the global mode's owner
            tracking_enforcer=tracking_enforcer
            if tracking_enforcer is not None
            else TrackingEnforcer(
                self.park, OperatingMode.TERRESTRIAL, settle_timeout_s=0, clock=FakeClock()
            ),
            get_left_frame=self.getter("left"),
            get_right_frame=self.getter("right"),
            wait_for_left_frame=self.waiter("left"),
            wait_for_right_frame=self.waiter("right"),
            settings=_SETTINGS,
            camera_geometry=lambda: cameras,
            runner=MountTestMoveRunner(clock=self.runner_clock),
        )
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


# ---------------------------------------------------------------------------------------------
# S6.0c -- Stop during an angular GOTO (#49; AGENTS.md "Manual movement versus measurement":
# bounded, cancellable, Qt event loop responsive), production adapters end-to-end (D1 included:
# no GUI-thread read queues behind the GOTO). The GOTO runs on the #51 simulator with a
# MANUAL FakeClock, so it stays in flight until the test advances time. A GUI-thread call that
# queues behind it would hang the test: a real-time watchdog then ends the GOTO (advances fake
# time) and the test fails on `fired` -- the watchdog is a safety bound, never the measurement.
# ---------------------------------------------------------------------------------------------

_WATCHDOG_S = 2.0


@contextmanager
def _watchdog(clock: FakeClock) -> Iterator[threading.Event]:
    fired = threading.Event()

    def fire() -> None:
        fired.set()
        clock.advance(60.0)  # ends the GOTO, so the blocked GUI call can return and fail

    timer = threading.Timer(_WATCHDOG_S, fire)
    timer.daemon = True
    timer.start()
    try:
        yield fired
    finally:
        timer.cancel()


def _until(condition: Callable[[], bool], timeout_s: float = 5.0) -> bool:
    """Barrier on another thread's progress (real time is only the safety bound)."""
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() >= deadline:
            return False
        threading.Event().wait(0.001)
    return True


def _run_fake_time_until_idle(clock: FakeClock, runner: MountTestMoveRunner) -> None:
    """Let every GOTO / stop confirmation in flight finish on fake time."""
    deadline = time.monotonic() + 10.0
    while runner.is_busy:
        assert time.monotonic() < deadline, "the runner never finished"
        clock.advance(30.0)
        threading.Event().wait(0.002)


class _StopRecorder:
    """Records on which thread OnStepAdapter's emergency stop was called."""

    def __init__(self, client: SimulatedOnStepIndiClient) -> None:
        self.threads: list[str] = []
        real = client.emergency_stop

        def stop(**kwargs: float) -> object:
            self.threads.append(threading.current_thread().name)
            return real(**kwargs)

        client.emergency_stop = stop  # type: ignore[method-assign]


def _sim_rig() -> tuple[_Rig, FakeClock, SimulatedOnStepIndiClient]:
    """Production adapters (pulse, park) over the #51 simulator on a manual FakeClock."""
    clock = FakeClock(auto_advance=False)
    rig = _Rig(clock=clock)
    assert isinstance(rig.client, SimulatedOnStepIndiClient)
    return rig, clock, rig.client


def _goto_in_flight_from_a_nudge(rig: _Rig, clock: FakeClock) -> MountTestMovePanel:
    """An uncalibrated RA+ nudge (300" at 0.1 deg/s) whose GOTO now blocks in OnStepAdapter."""
    panel = rig.panel()
    with _watchdog(clock) as fired:
        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
        deadline = time.monotonic() + 10.0
        while not panel._runner.is_busy and not fired.is_set():  # pre-move work, then runner
            assert time.monotonic() < deadline, panel._result_label.text()
            panel._poll()
    assert not fired.is_set(), "the nudge click blocked the GUI thread behind its own GOTO"
    assert clock.wait_for_sleepers(1), "the GOTO never started"
    return panel


class TestStopDuringABlockingAngularGoto:
    def test_runner_abort_sends_no_further_step_of_the_sequence(self) -> None:
        """A composed two-axis nudge: Stop during the first GOTO must not send the second."""
        rig, clock, client = _sim_rig()
        runner = MountTestMoveRunner()
        assert runner.submit_sequence(
            rig.park,
            rig.mount,
            [
                (MountAxis.AXIS1, AxisDirection.POSITIVE, 1000, 300.0),
                (MountAxis.AXIS2, AxisDirection.POSITIVE, 1000, 300.0),
            ],
            park_after=False,
        )
        assert clock.wait_for_sleepers(1)

        runner.abort()
        rig.mount.abort()
        _run_fake_time_until_idle(clock, runner)
        outcome = runner.take_latest()

        assert [axis for axis, _ in client.axis_move_calls] == ["ra"]  # Dec never sent
        assert outcome is not None and not outcome.pulsed
        assert outcome.error is not None and "stopped by the user" in outcome.error

    def test_stop_during_a_goto_returns_at_once_and_stops_the_mount_off_the_gui_thread(
        self, qapp: object
    ) -> None:
        rig, clock, client = _sim_rig()
        stops = _StopRecorder(client)
        panel = _goto_in_flight_from_a_nudge(rig, clock)

        with _watchdog(clock) as fired:
            panel._stop_button.click()
        reached = _until(lambda: client.axis_moves_aborted == 1, timeout_s=2.0)

        _run_fake_time_until_idle(clock, panel._runner)
        assert not fired.is_set(), "Stop blocked the GUI thread behind the running GOTO"
        assert reached, "OnStep's stop never reached the mount while the GOTO was running"
        assert stops.threads and "MainThread" not in stops.threads, stops.threads
        assert "Stopped by the user" in panel._result_label.text()
        panel.stop()

    def test_after_the_stopped_goto_ends_the_panel_is_idle_and_the_next_nudge_moves(
        self, qapp: object
    ) -> None:
        rig, clock, client = _sim_rig()
        panel = _goto_in_flight_from_a_nudge(rig, clock)
        with _watchdog(clock):
            panel._stop_button.click()
        _until(lambda: client.axis_moves_aborted == 1, timeout_s=2.0)
        _run_fake_time_until_idle(clock, panel._runner)
        panel._poll()

        assert panel.is_idle()
        assert panel._last_error == "stopped by the user"
        assert all(button.isEnabled() for button in panel._nudge_buttons["left"].values())
        ha_after_stop = client.ha_deg

        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
        deadline = time.monotonic() + 10.0
        while not panel._runner.is_busy:
            assert time.monotonic() < deadline, panel._result_label.text()
            panel._poll()
        _run_fake_time_until_idle(clock, panel._runner)
        _drain(panel)
        # the earlier Stop is not held against the operator's next action
        assert (client.ha_deg - ha_after_stop) * 3600.0 == pytest.approx(300.0)
        assert "Move failed" not in panel._result_label.text()
        panel.stop()

    def test_the_nudge_click_that_starts_a_goto_does_not_block_the_gui_thread(
        self, qapp: object
    ) -> None:
        """Found while characterizing S6.0c (D1): the click handler submits the move and then
        refreshes its buttons via `interface_state()` -> `OnStepMountParkAdapter.status()`,
        which queued behind the GOTO the click just started (and so did every poll tick)."""
        rig, clock, _client = _sim_rig()
        panel = rig.panel()
        with _watchdog(clock) as fired:
            panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
            deadline = time.monotonic() + 10.0
            while not panel._runner.is_busy and not fired.is_set():
                assert time.monotonic() < deadline, panel._result_label.text()
                panel._poll()
            if not fired.is_set():
                assert clock.wait_for_sleepers(1)
                panel._poll()  # one ordinary poll tick while the GOTO runs
        _run_fake_time_until_idle(clock, panel._runner)
        panel.stop()
        assert not fired.is_set()

    def test_a_park_panel_poll_during_a_goto_does_not_block_the_gui_thread(
        self, qapp: object
    ) -> None:
        rig, clock, _client = _sim_rig()
        park_panel = MountParkPanel(rig.park)
        park_panel._connect_button.setChecked(True)
        runner = MountTestMoveRunner()
        assert runner.submit_sequence(
            rig.park,
            rig.mount,
            [(MountAxis.AXIS1, AxisDirection.POSITIVE, 1000, 300.0)],
            park_after=False,
        )
        assert clock.wait_for_sleepers(1)
        with _watchdog(clock) as fired:
            park_panel._poll_status()
        _run_fake_time_until_idle(clock, runner)
        park_panel.stop()
        assert not fired.is_set()

    def test_a_focuser_panel_poll_during_a_goto_does_not_block_the_gui_thread(
        self, qapp: object
    ) -> None:
        """The OnStep focuser shares the mount's connection: FocuserPanel's poll (status() and
        is_moving()) must not wait out a Mount Align GOTO either."""
        rig, clock, _client = _sim_rig()
        focuser = OnStepFocuserAdapter(rig.mount._connection)
        focuser_panel = FocuserPanel(focuser)
        focuser_panel._connect_button.setChecked(True)
        runner = MountTestMoveRunner()
        assert runner.submit_sequence(
            rig.park,
            rig.mount,
            [(MountAxis.AXIS1, AxisDirection.POSITIVE, 1000, 300.0)],
            park_after=False,
        )
        assert clock.wait_for_sleepers(1)
        with _watchdog(clock) as fired:
            focuser_panel._poll_status()
        _run_fake_time_until_idle(clock, runner)
        focuser_panel.stop()
        assert not fired.is_set()
        assert "Position" in focuser_panel._status_label.text()


class TestTrackingDecisionsWhileTheMountIsBusy:
    """S6.0c review C1/C2 (#44), at the panels that decide: with another operation holding the
    OnStep connection, Mount Align's tracking check and the Mount panel's enforcement deny
    with the actionable busy reason -- never "verified" from a held-over or unknown reading --
    and return without waiting for that operation (watchdog: real-time safety bound only)."""

    @contextmanager
    def _connection_held(self, rig: _Rig) -> Iterator[threading.Event]:
        """Another thread holds the connection; if a GUI-thread call queues behind it, the
        watchdog releases it after 2 s so the test fails on `fired` instead of hanging."""
        connection = rig.mount._connection
        release, held, fired = threading.Event(), threading.Event(), threading.Event()

        def hold() -> None:
            with connection.operation_lock:
                held.set()
                if not release.wait(_WATCHDOG_S):
                    fired.set()

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5.0)
        try:
            yield fired
        finally:
            release.set()
            holder.join(5.0)

    @staticmethod
    def _tracking_rig(*, read_first: bool) -> _Rig:
        rig = _Rig(clock=FakeClock())
        rig.client.parked = False
        rig.client.tracking = False
        if read_first:
            assert rig.park.status().fresh
        rig.client.tracking = True  # real tracking ON, but no fresh reading can be taken
        return rig

    @pytest.mark.parametrize("with_enforcer", [True, False], ids=["enforcer", "no_enforcer"])
    def test_mount_align_tracking_check_denies_with_the_busy_reason(
        self, qapp: object, with_enforcer: bool
    ) -> None:
        """The panel reads the mount when it connects (tracking ON); with the connection held,
        that held-over reading must neither pass nor trigger a correction that queues behind
        the held operation. (A held-over OFF passing as "verified" is pinned at the enforcer:
        tests/core/mount/test_operating_mode.py.)"""
        rig = self._tracking_rig(read_first=False)
        enforcer = (
            TrackingEnforcer(rig.park, OperatingMode.TERRESTRIAL, settle_timeout_s=0)
            if with_enforcer
            else None
        )
        panel = rig.panel(tracking_enforcer=enforcer)
        with self._connection_held(rig) as fired:
            message = panel._tracking_failure_message(TrackingMode.OFF, "terrestrial")
        assert not fired.is_set(), "the tracking check queued behind the held connection"
        assert message is not None and MOUNT_BUSY_REASON in message
        assert rig.client.emergency_stop_calls == 0  # type: ignore[attr-defined]  # simulator client
        panel.stop()

    @pytest.mark.parametrize("read_first", [True, False], ids=["held_over", "nothing_read_yet"])
    def test_mount_panel_enforcement_records_a_denial_not_a_verification(
        self, qapp: object, read_first: bool
    ) -> None:
        rig = self._tracking_rig(read_first=read_first)
        enforcer = TrackingEnforcer(rig.park, OperatingMode.TERRESTRIAL, settle_timeout_s=0)
        park_panel = MountParkPanel(rig.park, tracking_enforcer=enforcer)
        with self._connection_held(rig) as fired:
            park_panel._enforce_tracking("unpark")
        assert not fired.is_set(), "the enforcement queued behind the held connection"
        assert not enforcer.measurement_allowed()
        assert enforcer.evidence()["last_reason"] == MOUNT_BUSY_REASON
        assert rig.client.emergency_stop_calls == 0  # type: ignore[attr-defined]  # simulator client


class TestWorkerDecisionsWaitBrieflyForAFreshReading:
    """S6.0c re-review R1: GUI polls hold `operation_lock` for a moment on every read, so the
    tracking check Mount Align's capture WORKER runs before every BEFORE/AFTER capture used to
    find it busy by plain collision -- a spurious "mount busy" calibration failure with nothing
    moving. Off the GUI thread a decision now waits briefly (bounded) for a fresh reading.
    `ObservableRLock` makes the collision deterministic: the lock is released exactly once the
    worker is provably waiting for it."""

    @staticmethod
    def _job(rig: _Rig) -> tuple[MountTestMovePanel, ObservableRLock, _CaptureJob]:
        rig.client.parked = False
        rig.client.tracking = False
        lock = install_observable_operation_lock(rig.mount._connection)
        enforcer = TrackingEnforcer(rig.park, OperatingMode.TERRESTRIAL, settle_timeout_s=0)
        panel = rig.panel(tracking_enforcer=enforcer)
        request = panel._snapshot_capture_request("terrestrial", "before", None, False)
        job = _CaptureJob(1, "before", request, lambda _result: None, time.monotonic())
        return panel, lock, job

    def test_a_momentary_poll_collision_does_not_fail_the_capture(self, qapp: object) -> None:
        rig = _Rig(clock=FakeClock())
        panel, lock, job = self._job(rig)
        held, release = threading.Event(), threading.Event()

        def poll_holding_the_lock() -> None:  # a GUI-thread read, frozen mid-read
            with lock:
                held.set()
                release.wait(5.0)

        poller = threading.Thread(target=poll_holding_the_lock, daemon=True)
        poller.start()
        assert held.wait(5.0)
        worker = threading.Thread(target=panel._run_capture_job, args=(job,), daemon=True)
        worker.start()
        outcome = lock.wait_until_blocked_or_done(worker)  # waiting for the poll to finish ...
        release.set()  # ... which it now does, well within the decision's wait
        worker.join(5.0)
        poller.join(5.0)

        assert outcome == "blocked", "the worker decided without waiting for a fresh reading"
        assert job.error is None and job.result is not None
        assert job.result.tracking_error is None, job.result.tracking_error
        panel.stop()

    def test_a_connection_held_beyond_the_wait_still_fails_closed(self, qapp: object) -> None:
        rig = _Rig(clock=FakeClock())
        panel, lock, job = self._job(rig)
        with self._held_for_a_goto(lock):
            started = time.monotonic()
            panel._run_capture_job(job)
            elapsed = time.monotonic() - started

        assert job.result is not None and job.result.tracking_error is not None
        assert MOUNT_BUSY_REASON in job.result.tracking_error
        from astrotool_core.mount.tracking_mode import WORKER_DECISION_FRESH_WAIT_S

        assert elapsed < WORKER_DECISION_FRESH_WAIT_S + 1.0  # bounded, not "until the GOTO ends"
        panel.stop()

    @staticmethod
    @contextmanager
    def _held_for_a_goto(lock: ObservableRLock) -> Iterator[None]:
        held, release = threading.Event(), threading.Event()

        def hold() -> None:
            with lock:
                held.set()
                release.wait(10.0)

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        assert held.wait(5.0)
        try:
            yield
        finally:
            release.set()
            holder.join(5.0)


class TestBusyIsShownAsBusy:
    """S6.0c re-review P-b/P-c: a busy mount is reported to the operator as busy -- never as
    "not available", and a busy denial of the Mount panel's enforcement is not dropped."""

    def test_mount_align_says_busy_not_unavailable_before_the_first_reading(
        self, qapp: object
    ) -> None:
        rig = _Rig(clock=FakeClock())
        panel = rig.panel()
        rig.park._last_status = None  # nothing read yet in this situation
        with TestTrackingDecisionsWhileTheMountIsBusy()._connection_held(rig):
            state = panel.interface_state()
        assert (state.available, state.code) == (False, "mount_busy")
        assert "busy" in state.detail and "not available" not in state.detail
        panel.stop()

    def test_the_mount_panel_shows_a_busy_denial(self, qapp: object) -> None:
        rig = _Rig(clock=FakeClock())
        enforcer = TrackingEnforcer(rig.park, OperatingMode.TERRESTRIAL, settle_timeout_s=0)
        park_panel = MountParkPanel(rig.park, tracking_enforcer=enforcer)
        with TestTrackingDecisionsWhileTheMountIsBusy()._connection_held(rig):
            park_panel._enforce_tracking("unpark")
        assert MOUNT_BUSY_REASON in park_panel._policy_suffix(False)
