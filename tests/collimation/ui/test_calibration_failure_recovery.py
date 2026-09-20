"""Issue #49: every Mount Align calibration failure must leave the application responsive
and retryable -- and no hardware/frame wait may run on the Qt GUI thread.

Field bundle 6d33f37c (the "frozen UI"): each BEFORE/AFTER capture waited 24-60 s for fresh
frames (Guide delivered them ~5 s apart, Main never did) inside a click/timer handler, so the
event loop was blocked for minutes with no status text, and the operator retried again and again.

The rig feeds the REAL `acquire_stable_frame` (exposure-start freshness) with frame streams whose
exposure time makes captures take real (short) time; the panel runs in threaded-capture mode with
its real QTimer, driven by `QTest.qWait` (the real event loop). A heartbeat QTimer proves the
loop is never blocked while a capture is in flight."""

from __future__ import annotations

import time
from collections.abc import Callable

import numpy as np
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
    acquire_stable_frame,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, MountAxis
from astrotool_core.mount.movement_sizing import CameraGeometry
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.ui import mount_test_move_runner as runner_module
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest

_EXPOSURE_S = 0.12  # a capture window (3 fresh frames) takes ~0.5 s: long enough to observe
_SETTINGS = MountAlignmentSettings(
    settle_ms=0,
    frame_settle_ms=0,
    stability_sample_interval_s=0.0,
    stability_timeout_s=2.0,
    pulse_ms=1000,
)
_SHAPES = {"left": (120, 200), "right": (100, 300)}


def _texture(seed: int, shape: tuple[int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random(shape).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0, dtype=np.float32)


class _Rig:
    """Frame streams driven by the mount's cumulative pulses; failure modes are plain flags."""

    def __init__(self, mount: FakeMountAdapter) -> None:
        self.mount = mount
        self.px_per_ms = {"left": 0.05, "right": 0.04}
        self.base = {k: _texture(i + 1, _SHAPES[k]) for i, k in enumerate(("left", "right"))}
        self.alt = {k: _texture(i + 50, _SHAPES[k]) for i, k in enumerate(("left", "right"))}
        self.never_fresh: dict[str, bool] = {"left": False, "right": False}
        self.jitter: dict[str, Callable[[int], int] | None] = {"left": None, "right": None}
        self.scramble_after_pulse = False
        self.raise_in_waiter = False
        self.draws = {"left": 0, "right": 0}

    def clean(self, key: str, extra: int = 0) -> np.ndarray:
        dx = dy = 0.0
        for axis, direction, ms in self.mount.pulse_log:
            signed = ms * self.px_per_ms[key] * (1 if direction is AxisDirection.POSITIVE else -1)
            if axis is MountAxis.AXIS1:
                dx += signed
            else:
                dy += signed
        scrambled = self.scramble_after_pulse and bool(self.mount.pulse_log)
        base = self.alt[key] if scrambled else self.base[key]
        return np.roll(np.roll(base, round(dy), axis=0), round(dx) + extra, axis=1)

    def waiter(self, key: str) -> Callable[[float, float], FrameAcquisitionResult]:
        def wait(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            if self.raise_in_waiter:
                raise RuntimeError("camera SDK exploded while waiting for a frame")
            if self.never_fresh[key]:
                time.sleep(min(timeout_s, 0.35))  # the field case: a fresh frame never arrives
                return FrameAcquisitionResult(FrameAcquisitionStatus.EXPOSURE_OVERLAPPED_MOTION)

            def next_frame(_remaining: float) -> DeliveredFrame | None:
                time.sleep(_EXPOSURE_S)  # a real exposure: starts now, delivered when it ends
                self.draws[key] += 1
                jitter = self.jitter[key]
                extra = jitter(self.draws[key]) if jitter is not None else 0
                return DeliveredFrame(self.clean(key, extra), time.monotonic(), _EXPOSURE_S)

            return acquire_stable_frame(
                next_frame,
                is_available=lambda: True,
                reference_monotonic=reference,
                timeout_s=timeout_s,
            )

        return wait

    def latest(self, key: str) -> Callable[[], np.ndarray]:
        return lambda: self.clean(key)


class _Heartbeat:
    """Counts ticks of an ordinary QTimer: if the GUI thread is blocked, it stops counting."""

    def __init__(self) -> None:
        self.ticks = 0
        self._timer = QTimer()
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self.ticks += 1

    def stop(self) -> None:
        self._timer.stop()


def _panel(
    rig: _Rig,
    *,
    park: FakeMountPark | None = None,
    enforcer: TrackingEnforcer | None = None,
    geometry: Callable[[], list[CameraGeometry]] | None = None,
    settings: MountAlignmentSettings = _SETTINGS,
) -> MountTestMovePanel:
    rig.mount.connect()
    panel = MountTestMovePanel(
        rig.mount,
        mount_park=park if park is not None else FakeMountPark(start_parked=True),
        get_left_frame=rig.latest("left"),
        get_right_frame=rig.latest("right"),
        wait_for_left_frame=rig.waiter("left"),
        wait_for_right_frame=rig.waiter("right"),
        settings=settings,
        tracking_enforcer=enforcer,
        camera_geometry=geometry,
        threaded_captures=True,
    )
    panel._terrestrial_button.click()
    panel._connect_button.setChecked(True)
    panel._timer.setInterval(20)  # the real poll timer, just faster than production's 250 ms
    return panel


def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        QTest.qWait(15)  # runs the REAL event loop: timers, queued signals, repaints
    return predicate()


def _wait_idle(panel: MountTestMovePanel, *, timeout_s: float = 30.0) -> None:
    assert _wait_until(panel.is_idle, timeout_s=timeout_s), (
        f"panel never returned to idle: {panel.diagnostic_activity()}"
    )


def _assert_recovered(panel: MountTestMovePanel) -> None:
    """The issue's invariant after ANY failure."""
    activity = panel.diagnostic_activity()
    assert panel.is_idle(), activity
    assert activity["capture_in_flight"] is False
    assert activity["runner_busy"] is False
    assert activity["calibration_queue_len"] == 0
    assert panel._run_calibration_button.isEnabled()  # a second Run Calibration is possible
    assert not panel._stop_button.isEnabled()
    assert panel._result_label.text().strip() != ""  # an explicit reason/outcome is visible


class TestTheEventLoopStaysResponsive:
    def test_run_calibration_returns_at_once_and_the_loop_keeps_ticking(
        self, qapp: object
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig)
        heartbeat = _Heartbeat()

        started = time.monotonic()
        panel._run_calibration_button.click()
        click_seconds = time.monotonic() - started

        assert click_seconds < 0.3  # the click handler no longer waits for frames
        assert panel.diagnostic_activity()["capture_in_flight"] is True
        assert not panel._run_calibration_button.isEnabled()
        assert panel._stop_button.isEnabled()
        ticks_before = heartbeat.ticks
        QTest.qWait(400)  # the capture is still running (a window takes ~0.5 s)
        assert heartbeat.ticks - ticks_before >= 15  # the event loop was NOT blocked
        text = panel._result_label.text().lower()
        assert "capturing" in text and "fresh" in text  # visible progress, not a silent freeze
        heartbeat.stop()
        panel._on_stop()
        panel.stop()

    def test_the_progress_text_reports_elapsed_time(self, qapp: object) -> None:
        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig)
        panel._run_calibration_button.click()
        first = panel._result_label.text()

        QTest.qWait(350)

        assert panel._result_label.text() != first  # the elapsed counter moved
        assert " s" in panel._result_label.text()
        panel._on_stop()
        panel.stop()

    def test_a_nudge_capture_is_off_the_gui_thread_too(self, qapp: object) -> None:
        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig)
        heartbeat = _Heartbeat()

        started = time.monotonic()
        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)

        assert time.monotonic() - started < 0.3
        QTest.qWait(300)
        assert heartbeat.ticks >= 10
        _wait_idle(panel)
        assert rig.mount.pulse_log  # ... and the nudge itself still completed
        heartbeat.stop()
        panel.stop()


def _setup_capture_timeout(rig: _Rig, panel: MountTestMovePanel, park: FakeMountPark) -> None:
    rig.never_fresh["left"] = True  # the bundle: Main never delivers a fresh frame
    panel._fresh_frame_timeout_s = lambda: 0.3  # type: ignore[method-assign]


def _setup_unstable(rig: _Rig, panel: MountTestMovePanel, park: FakeMountPark) -> None:
    rig.jitter = {"left": lambda d: 25 * d, "right": lambda d: 25 * d}
    panel._fresh_frame_timeout_s = lambda: 0.3  # type: ignore[method-assign]


def _setup_match_failure(rig: _Rig, panel: MountTestMovePanel, park: FakeMountPark) -> None:
    rig.scramble_after_pulse = True  # AFTER frames show unrelated content: no match


def _setup_no_motion(rig: _Rig, panel: MountTestMovePanel, park: FakeMountPark) -> None:
    rig.px_per_ms = {"left": 0.0, "right": 0.0}


def _setup_capture_worker_exception(
    rig: _Rig, panel: MountTestMovePanel, park: FakeMountPark
) -> None:
    rig.raise_in_waiter = True


def _setup_nothing(rig: _Rig, panel: MountTestMovePanel, park: FakeMountPark) -> None:
    """The fault is built into the mount itself (see `_SCENARIOS`)."""


def _plain_mount() -> FakeMountAdapter:
    return FakeMountAdapter()


def _rejecting_mount() -> FakeMountAdapter:
    # the runner retries a rejected pulse 6 times: the first 6 attempts are all refused, so the
    # first calibration step fails; later attempts (the retry) are accepted
    return FakeMountAdapter(reject_first_n_pulses=6)


_SCENARIOS: dict[
    str,
    tuple[
        Callable[[], FakeMountAdapter],
        Callable[[_Rig, MountTestMovePanel, FakeMountPark], None],
    ],
] = {
    "capture_timeout": (_plain_mount, _setup_capture_timeout),
    "unstable_image": (_plain_mount, _setup_unstable),
    "match_failure": (_plain_mount, _setup_match_failure),
    "capture_worker_exception": (_plain_mount, _setup_capture_worker_exception),
    "mount_command_rejection": (_rejecting_mount, _setup_nothing),
}


class TestEveryFailureReturnsToIdle:
    @pytest.mark.parametrize("scenario", list(_SCENARIOS))
    def test_failure_then_a_second_run_without_restarting(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, scenario: str
    ) -> None:
        monkeypatch.setattr(runner_module, "_PULSE_REJECTION_RETRY_DELAY_S", 0.01)
        make_mount, setup = _SCENARIOS[scenario]
        rig = _Rig(make_mount())
        park = FakeMountPark(start_parked=True)
        panel = _panel(rig, park=park)
        setup(rig, panel, park)
        heartbeat = _Heartbeat()

        panel._run_calibration_button.click()
        _wait_idle(panel)

        _assert_recovered(panel)
        assert heartbeat.ticks > 0  # the loop kept running the whole time
        assert panel.calibration_for("left") is None
        # ... and the application recovers: undo the fault, run again, no restart needed.
        rig.never_fresh = {"left": False, "right": False}
        rig.jitter = {"left": None, "right": None}
        rig.scramble_after_pulse = False
        rig.raise_in_waiter = False
        rig.mount.pulse_log.clear()
        panel._fresh_frame_timeout_s = lambda: 5.0  # type: ignore[method-assign]
        panel._run_calibration_button.click()
        assert panel.diagnostic_activity()["capture_in_flight"] is True
        assert _wait_until(lambda: bool(rig.mount.pulse_log), timeout_s=15.0)  # it really runs
        panel._on_stop()
        _wait_idle(panel)
        heartbeat.stop()
        panel.stop()


class TestOtherFailureClasses:
    def test_no_motion_is_reported_and_the_panel_is_reusable(self, qapp: object) -> None:
        rig = _Rig(FakeMountAdapter())
        rig.px_per_ms = {"left": 0.0, "right": 0.0}
        panel = _panel(rig)

        panel._run_calibration_button.click()
        _wait_idle(panel, timeout_s=60.0)

        _assert_recovered(panel)
        assert panel.calibration_for("left") is None
        assert "no motion" in panel._result_label.text().lower()
        panel.stop()

    def test_a_tracking_state_failure_is_reported_and_recoverable(self, qapp: object) -> None:
        rig = _Rig(FakeMountAdapter())
        park = FakeMountPark(start_parked=False, refuse_stop_tracking=True)
        park.start_tracking()  # ON, and the mount refuses to turn it OFF
        enforcer = TrackingEnforcer(park, OperatingMode.TERRESTRIAL, settle_timeout_s=0.05)
        panel = _panel(rig, park=park, enforcer=enforcer)

        panel._run_calibration_button.click()
        _wait_idle(panel)

        _assert_recovered(panel)
        assert "tracking" in panel._result_label.text().lower()
        assert rig.mount.pulse_log == []  # nothing was ever commanded
        panel.stop()

    def test_a_crash_of_the_runner_worker_is_reported_and_recoverable(
        self, qapp: object
    ) -> None:
        class _RaisingPark(FakeMountPark):
            def unpark(self) -> None:
                raise ConnectionError("indiserver went away during unpark")

        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig, park=_RaisingPark(start_parked=True))

        panel._run_calibration_button.click()
        _wait_idle(panel)

        _assert_recovered(panel)
        assert "worker crashed" in panel._result_label.text()
        panel.stop()

    def test_an_exception_in_a_continuation_resets_the_state_and_is_re_raised(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig)
        panel._timer.stop()  # drive _poll by hand so the exception is observable

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("bug in the calibration step")

        monkeypatch.setattr(panel, "_after_before_capture", boom)
        panel._run_calibration_button.click()
        assert _wait_until(
            lambda: panel._capture_job is not None and panel._capture_job.done.is_set()
        )

        with pytest.raises(RuntimeError, match="bug in the calibration step"):
            panel._poll()  # ... re-raised so the app's excepthook still records an incident

        _assert_recovered(panel)
        internal = str(panel.diagnostic_activity()["last_internal_error"])
        assert "bug in the calibration step" in internal
        assert panel.diagnostic_activity()["last_cleanup_ok"] is True
        panel.stop()

    def test_a_failure_while_sizing_the_move_leaves_nothing_open(self, qapp: object) -> None:
        def broken_geometry() -> list[CameraGeometry]:
            raise RuntimeError("optics lookup failed")

        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig, geometry=broken_geometry)

        with pytest.raises(RuntimeError, match="optics lookup failed"):
            panel._run_calibration_button.click()
            panel._on_run_calibration_clicked()  # click() swallows slot exceptions; call directly

        _assert_recovered(panel)
        assert panel._calibration_queue == []
        panel.stop()


class TestStop:
    def test_stop_during_a_capture_returns_to_idle_at_once_and_nothing_runs_later(
        self, qapp: object
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig)
        panel._run_calibration_button.click()
        assert panel.diagnostic_activity()["capture_in_flight"] is True

        panel._stop_button.click()

        assert panel.is_idle()  # immediately, not after the slow wait finishes
        _assert_recovered(panel)
        QTest.qWait(900)  # the abandoned worker finishes in the background ...
        assert rig.mount.pulse_log == []  # ... and its stale result is discarded
        assert panel.is_idle()
        panel.stop()

    def test_stop_then_a_new_run_works(self, qapp: object) -> None:
        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig)
        panel._run_calibration_button.click()
        panel._stop_button.click()

        panel._run_calibration_button.click()

        assert panel.diagnostic_activity()["capture_in_flight"] is True
        assert _wait_until(lambda: bool(rig.mount.pulse_log), timeout_s=15.0)
        panel._on_stop()
        _wait_idle(panel)
        panel.stop()

    def test_stop_while_the_mount_is_moving_drops_the_calibration(self, qapp: object) -> None:
        class _SlowPark(FakeMountPark):
            def unpark(self) -> None:
                time.sleep(0.5)  # the runner worker stays busy for a while
                super().unpark()

        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig, park=_SlowPark(start_parked=True))
        panel._run_calibration_button.click()
        assert _wait_until(lambda: panel.diagnostic_activity()["runner_busy"], timeout_s=10.0)

        panel._stop_button.click()
        _wait_idle(panel, timeout_s=10.0)

        assert panel.diagnostic_activity()["calibration_queue_len"] == 0
        QTest.qWait(300)
        assert len(rig.mount.pulse_log) <= 1  # the calibration did not carry on after Stop
        assert panel._run_calibration_button.isEnabled()
        panel.stop()


class TestBundle6d33f37cRegression:
    def test_a_camera_that_never_delivers_a_fresh_frame_can_be_retried_over_and_over(
        self, qapp: object
    ) -> None:
        """The field case: Main's frames never satisfy the freshness rule, Guide's do; the
        operator clicked Run Calibration / nudges again and again. Every attempt must fail
        VISIBLY, return to idle, and keep the event loop alive -- never freeze."""
        rig = _Rig(FakeMountAdapter())
        rig.never_fresh["left"] = True
        panel = _panel(rig)
        panel._fresh_frame_timeout_s = lambda: 0.3  # type: ignore[method-assign]
        heartbeat = _Heartbeat()

        for attempt in range(3):
            ticks_before = heartbeat.ticks
            panel._run_calibration_button.click()
            _wait_idle(panel)
            _assert_recovered(panel)
            assert heartbeat.ticks > ticks_before, f"attempt {attempt}: event loop blocked"
            assert "no frame available" in panel._result_label.text().lower()
            assert rig.mount.pulse_log == []  # failed at the first BEFORE capture, as in the bundle

        heartbeat.stop()
        panel.stop()


class TestDiagnostics:
    def test_activity_reports_state_capture_runner_and_timer(self, qapp: object) -> None:
        rig = _Rig(FakeMountAdapter())
        panel = _panel(rig)

        idle = panel.diagnostic_activity()
        assert idle["state"] == "idle"
        assert idle["poll_timer_active"] is True

        panel._run_calibration_button.click()
        busy = panel.diagnostic_activity()
        assert busy["state"] == "capturing"
        assert busy["capture_in_flight"] is True
        assert busy["capture_label"] == "axis1_positive_first_before"
        assert busy["capture_elapsed_s"] >= 0.0
        assert panel.diagnostic_context()["activity"]["state"] == "capturing"
        panel._on_stop()
        panel.stop()


class TestMainWindowWiring:
    def test_the_window_can_enable_threaded_captures_and_defaults_to_the_synchronous_mode(
        self, qapp: object
    ) -> None:
        from astrotool_core.camera.replay_camera import ReplayCamera
        from collimation_tool.ui.main_window import MainWindow

        image = np.full((60, 80), 100.0, dtype=np.float32)

        def make(*, threaded: bool | None = None) -> MainWindow:
            camera = ReplayCamera.from_arrays([image], cycle=True)
            if threaded is None:
                return MainWindow(camera, device_lister=lambda: [])
            return MainWindow(camera, device_lister=lambda: [], threaded_captures=threaded)

        assert make()._test_move_panel._threaded_captures is False  # existing callers unchanged
        assert make(threaded=True)._test_move_panel._threaded_captures is True
