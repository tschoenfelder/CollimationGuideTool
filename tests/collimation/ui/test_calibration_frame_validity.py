"""Issue #43: terrestrial Mount Align must never turn a movement-overlapping,
stale or unstable frame into calibration evidence -- and must report a
commanded move that produced NO image displacement explicitly.

Field bundle fe91004f (terrestrial): 8 pulses of 500 ms, every stability record
'stable / 0.0 px', every measured response exactly (0, 0) on both cameras ->
'calibration_invalid'. The frames were NOT motion blurred (crisp Main frame,
zero-lag correlation 0.75-0.99 between consecutive frames): the mount produced
no measurable image motion. That case is the last test class here.

The rig feeds the REAL `acquire_stable_frame` (exposure-start freshness) with a
scripted frame stream per camera, so an overlapping/stale exposure is judged by
production code; every array that reaches the translation estimator is recorded,
and 'poisoned' frames carry a marker pixel so a leak is impossible to miss."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    acquire_stable_frame,
)
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, MountAxis
from astrotool_core.mount.movement_sizing import CameraGeometry
from astrotool_core.target.translation_offset import measure_translation_offset
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.ui import mount_test_move_panel as panel_module
from collimation_tool.ui.mount_test_move_panel import (
    MeasurementFailureClass,
    MountTestMovePanel,
)

_MARKER = 60000.0
_EXPOSURE_S = 0.005
_SETTINGS = MountAlignmentSettings(
    settle_ms=0,
    frame_settle_ms=0,
    stability_sample_interval_s=0.0,
    stability_timeout_s=1.5,
    pulse_ms=1000,
)
_SHAPES = {"left": (120, 200), "right": (100, 300)}
_PX_PER_MS = {"left": 0.05, "right": 0.04}  # 1000 ms -> 50 px (25%) / 40 px


def _texture(seed: int, shape: tuple[int, int]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = rng.random(shape).astype(np.float32)
    smooth = sum(np.roll(np.roll(base, i, 0), j, 1) for i in range(4) for j in range(4))
    return np.asarray(smooth / 16.0 * 1000.0 + 100.0, dtype=np.float32)


class _Rig:
    """Per-camera frame streams driven by the mount's cumulative pulses.

    `bad[key]` = number of poisoned frames ('overlap' / 'stale') delivered BEFORE the
    clean ones, on EVERY new capture request. `motion[key]` = a callable
    (draw_index -> pixel shift) making frames keep moving (instability)."""

    def __init__(
        self,
        mount: FakeMountAdapter,
        *,
        px_per_ms: dict[str, float] | None = None,
        dead_moves: frozenset[tuple[MountAxis, AxisDirection]] = frozenset(),
    ) -> None:
        self.mount = mount
        #: (axis, direction) pairs the driver ACCEPTS but that move nothing (field: 0270868c)
        self.dead_moves = dead_moves
        self.px_per_ms = px_per_ms or dict(_PX_PER_MS)
        self.base = {k: _texture(i + 1, _SHAPES[k]) for i, k in enumerate(("left", "right"))}
        self.bad_kind: dict[str, str | None] = {"left": None, "right": None}
        self.bad_count = {"left": 2, "right": 2}
        self.jitter: dict[str, Callable[[int], int] | None] = {"left": None, "right": None}
        self.draws = {"left": 0, "right": 0}
        self._served_bad = {"left": 0, "right": 0}
        self.latest_calls = 0

    def _shift(self, key: str) -> tuple[int, int]:
        dx = dy = 0.0
        for axis, direction, ms in self.mount.pulse_log:
            if (axis, direction) in self.dead_moves:
                continue
            signed = ms * self.px_per_ms[key] * (1 if direction is AxisDirection.POSITIVE else -1)
            if axis is MountAxis.AXIS1:
                dx += signed
            else:
                dy += signed
        return round(dy), round(dx)

    def clean(self, key: str, extra_dx: int = 0) -> np.ndarray:
        dy, dx = self._shift(key)
        return np.roll(np.roll(self.base[key], dy, axis=0), dx + extra_dx, axis=1)

    def poisoned(self, key: str) -> np.ndarray:
        frame = np.roll(self.clean(key), 37, axis=1).copy()  # visibly displaced content
        frame[0, 0] = _MARKER
        return frame

    def waiter(self, key: str) -> Callable[[float, float], FrameAcquisitionResult]:
        def wait(reference: float, timeout_s: float) -> FrameAcquisitionResult:
            self._served_bad[key] = 0

            def next_frame(_remaining: float) -> DeliveredFrame | None:
                kind = self.bad_kind[key]
                if kind is not None and self._served_bad[key] < self.bad_count[key]:
                    self._served_bad[key] += 1
                    if kind == "overlap":  # delivered after the move, EXPOSED during it
                        return DeliveredFrame(self.poisoned(key), time.monotonic(), 5.0)
                    return DeliveredFrame(self.poisoned(key), reference - 1.0, 0.01)  # stale
                # a real exposure: it starts now and the frame is delivered when it ends
                time.sleep(_EXPOSURE_S)
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
        def get() -> np.ndarray:
            self.latest_calls += 1
            frame = self.clean(key).copy()
            frame[0, 0] = _MARKER  # 'the displayed frame' -- must never be measurement evidence
            return frame

        return get


class _Spy:
    """Records every array handed to the production translation estimator."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.seen: list[np.ndarray] = []
        self.panel_calls = 0
        from astrotool_core.acquisition import image_stability

        real_tier = image_stability.measure_translation_offset_with_tier

        def spy_tier(before: np.ndarray, after: np.ndarray, **kw: Any) -> Any:  # noqa: ANN401
            self.seen.extend([before, after])
            return real_tier(before, after, **kw)

        def spy_panel(before: np.ndarray, after: np.ndarray, **kw: Any) -> Any:  # noqa: ANN401
            self.seen.extend([before, after])
            self.panel_calls += 1
            return measure_translation_offset(before, after, **kw)

        monkeypatch.setattr(image_stability, "measure_translation_offset_with_tier", spy_tier)
        monkeypatch.setattr(panel_module, "measure_translation_offset", spy_panel)

    @property
    def poisoned_seen(self) -> int:
        return sum(1 for array in self.seen if array[0, 0] == _MARKER)


def _panel(
    rig: _Rig,
    *,
    geometry: list[CameraGeometry] | None = None,
    settings: MountAlignmentSettings = _SETTINGS,
) -> MountTestMovePanel:
    rig.mount.connect()
    panel = MountTestMovePanel(
        rig.mount,
        mount_park=FakeMountPark(start_parked=True),
        get_left_frame=rig.latest("left"),
        get_right_frame=rig.latest("right"),
        wait_for_left_frame=rig.waiter("left"),
        wait_for_right_frame=rig.waiter("right"),
        settings=settings,
        camera_geometry=(lambda: geometry) if geometry is not None else None,
    )
    panel._terrestrial_button.click()
    panel._connect_button.setChecked(True)
    return panel


def _drive(panel: MountTestMovePanel, *, timeout_s: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_s
    while panel._calibration_queue or panel._pending is not None or panel._runner.is_busy:
        while panel._runner.is_busy:
            assert time.monotonic() < deadline, "never completed"
            time.sleep(0.005)
        panel._poll()
        assert time.monotonic() < deadline, "never completed"


def _run(panel: MountTestMovePanel) -> None:
    panel._run_calibration_button.click()
    _drive(panel)


class TestOverlappingAndStaleFramesAreNeverMeasured:
    @pytest.mark.parametrize("kind", ["overlap", "stale"])
    def test_a_poisoned_frame_never_reaches_the_estimator(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch, kind: str
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        rig.bad_kind = {"left": kind, "right": kind}
        spy = _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        assert spy.poisoned_seen == 0  # delivered after the move / exposed during it: rejected
        matrix = panel.calibration_for("left")
        assert matrix is not None
        response = matrix.response_for(MountAxis.AXIS1, AxisDirection.POSITIVE)
        assert abs(abs(response.dx_px) - 50) <= 1  # the CLEAN frames' true shift, not +37 junk
        panel.stop()

    def test_the_first_clean_post_settle_exposure_is_accepted(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        spy = _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        assert panel.calibration_for("left") is not None
        assert spy.poisoned_seen == 0
        panel.stop()

    def test_the_latest_displayed_frame_is_never_measurement_evidence(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        spy = _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        assert rig.latest_calls == 0  # the display getter is not even consulted
        assert spy.poisoned_seen == 0
        panel.stop()


class TestStability:
    def test_frames_still_moving_after_the_move_keep_the_wait_going(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        # the image keeps drifting 20 px per draw for the first few draws, then settles
        rig.jitter = {
            "left": lambda draw: 20 * draw if draw <= 4 else 80,
            "right": lambda draw: 20 * draw if draw <= 4 else 80,
        }
        _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        assert panel.calibration_for("left") is not None  # waited, then measured
        assert rig.draws["left"] > 3
        panel.stop()

    def test_stability_never_reached_fails_explicitly_and_never_measures(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        rig.jitter = {"left": lambda d: 25 * d, "right": lambda d: 25 * d}  # always moving
        spy = _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        assert rig.mount.pulse_log == []  # an unstable BEFORE: the step never started
        assert spy.panel_calls == 0  # the response estimator was never called
        assert panel.calibration_for("left") is None and panel.calibration_for("right") is None
        assert "failed" in panel._result_label.text().lower()
        classes = panel._last_failure_classes
        assert MeasurementFailureClass.IMAGE_NOT_STABLE in classes.values()
        panel.stop()

    def test_one_camera_unstable_one_stable_excludes_only_the_unstable_one(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        rig.jitter = {"left": lambda d: 25 * d, "right": None}  # only Main keeps moving
        _Spy(monkeypatch)
        quick = MountAlignmentSettings(
            settle_ms=0,
            frame_settle_ms=0,
            stability_sample_interval_s=0.0,
            stability_timeout_s=0.4,
            pulse_ms=1000,
        )
        panel = _panel(rig, settings=quick)
        panel._fresh_frame_timeout_s = lambda: 0.2  # type: ignore[method-assign]

        _run(panel)

        assert panel.calibration_for("left") is None  # excluded (documented per-camera policy)
        assert panel.calibration_for("right") is not None  # the stable camera still calibrates
        assert panel._last_failure_classes["left"] is MeasurementFailureClass.IMAGE_NOT_STABLE
        panel.stop()


class TestNudgeBeforeIsVerified:
    def test_a_nudge_never_uses_the_latest_displayed_frame(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        spy = _Spy(monkeypatch)
        panel = _panel(rig)

        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
        _drive(panel)

        assert rig.latest_calls == 0  # the displayed frame was never the BEFORE
        assert spy.poisoned_seen == 0
        assert rig.draws["left"] >= _SETTINGS.stability_sample_count  # a verified capture ran
        panel.stop()

    def test_a_verified_at_rest_reference_is_reused_only_until_the_mount_is_commanded(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        _Spy(monkeypatch)
        panel = _panel(rig)
        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
        _drive(panel)  # its verified AFTER is now the at-rest reference

        assert panel._reusable_reference("terrestrial") is not None  # nothing commanded since
        assert panel._runner.submit(  # type: ignore[attr-defined]
            panel._mount_park, panel._mount, MountAxis.AXIS1, AxisDirection.POSITIVE, 50
        )
        _drive(panel)
        assert panel._reusable_reference("terrestrial") is None  # a pulse invalidated it
        panel.stop()

    def test_an_old_reference_is_not_reused(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        _Spy(monkeypatch)
        panel = _panel(rig)
        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
        _drive(panel)
        reference = panel._verified_reference
        assert reference is not None
        object.__setattr__(reference, "taken_at", time.monotonic() - 3600.0)

        assert panel._reusable_reference("terrestrial") is None
        panel.stop()


class TestDiagnosticsTimeline:
    def test_every_capture_leaves_reference_pulse_and_frame_timing_evidence(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter())
        _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        timeline = panel.diagnostic_capture_timeline()
        assert len(timeline) >= 16  # BEFORE + AFTER of the 8 steps (not just the last capture)
        after = next(entry for entry in timeline if entry["label"].endswith("_after"))
        assert after["pulse"]["motion_started_at"] is not None
        assert after["pulse"]["motion_ended_at"] >= after["pulse"]["motion_started_at"]
        camera = after["cameras"]["left"]
        timings = camera["frame_timings"]
        assert timings
        for timing in timings:  # every accepted exposure STARTED after the reference
            assert timing["exposure_start_monotonic"] >= timing["reference_monotonic"]
        assert camera["min_score"] is not None and camera["min_score"] > 0.15
        assert camera["tiers"]
        panel.stop()

    def test_the_main_window_bundle_carries_the_timeline(self, qapp: object) -> None:
        from astrotool_core.camera.replay_camera import ReplayCamera
        from collimation_tool.ui.main_window import MainWindow

        window = MainWindow(
            ReplayCamera.from_arrays([np.full((60, 80), 100.0, dtype=np.float32)], cycle=True),
            device_lister=lambda: [],
        )
        window._test_move_panel._capture_timeline.append({"label": "x", "cameras": {}})

        assert window._diagnostic_context()["mount_test_move_timeline"][0]["label"] == "x"


class TestNoMotionIsExplicit:
    """The field bundle fe91004f case: stable frames, confident matches, image did not move."""

    def test_a_static_scene_after_commanded_moves_is_reported_and_never_a_matrix(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter(), px_per_ms={"left": 0.0, "right": 0.0})
        _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        assert panel.calibration_for("left") is None and panel.calibration_for("right") is None
        text = panel._result_label.text().lower()
        assert "measured no motion on either camera" in text  # per-axis, cross-camera message
        assert "mount" in text  # the actionable hint
        # the frames were fine (stable, confident): NOT an unstable/invalid-capture failure
        classes = set(panel._last_failure_classes.values())
        assert MeasurementFailureClass.IMAGE_NOT_STABLE not in classes
        assert MeasurementFailureClass.CAPTURE_INVALID not in classes
        panel.stop()

    def test_the_mount_is_returned_after_a_no_motion_run(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter(), px_per_ms={"left": 0.0, "right": 0.0})
        _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        net = sum(
            d if direction is AxisDirection.POSITIVE else -d
            for _a, direction, d in rig.mount.pulse_log
        )
        assert net == 0
        panel.stop()

    def test_one_dead_direction_is_never_hidden_by_the_working_ones(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RA+/RA-/Dec+ move the image, Dec- is accepted by the driver but moves nothing: the
        two POSITIVE responses alone (all is_degenerate() looks at) would look fine."""
        dead = frozenset({(MountAxis.AXIS2, AxisDirection.NEGATIVE)})
        rig = _Rig(FakeMountAdapter(), dead_moves=dead)
        _Spy(monkeypatch)
        panel = _panel(rig)

        _run(panel)

        assert panel.calibration_for("left") is None  # not a usable 4-direction matrix
        assert panel._last_failure_classes["left"] is MeasurementFailureClass.NO_MOTION_DETECTED
        assert "at least one direction produced no image displacement" in panel._result_label.text()
        panel.stop()

    def test_a_nudge_that_moved_nothing_is_not_a_confirmed_displacement(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter(), px_per_ms={"left": 0.0, "right": 0.0})
        _Spy(monkeypatch)
        panel = _panel(rig)

        panel._on_nudge_clicked("left", MountAxis.AXIS1, AxisDirection.POSITIVE)
        _drive(panel)

        assert panel._last_failure_classes["left"] is MeasurementFailureClass.NO_MOTION_DETECTED
        assert "not confirmed" in panel._result_label.text().lower()
        panel.stop()

    def test_sized_calibration_grows_the_move_before_declaring_no_motion(
        self, qapp: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rig = _Rig(FakeMountAdapter(), px_per_ms={"left": 0.0, "right": 0.0})
        _Spy(monkeypatch)
        geometry = [CameraGeometry("left", 200, 120, 3.0), CameraGeometry("right", 300, 100, 12.0)]
        panel = _panel(rig, geometry=geometry)

        _run(panel)

        durations = [d for _a, _dr, d in rig.mount.pulse_log]
        assert max(durations) > durations[0]  # #46 adaptive growth tried first ...
        assert max(durations) <= 3000  # ... within the envelope
        assert panel._last_failure_classes["left"] is MeasurementFailureClass.NO_MOTION_DETECTED
        assert panel.calibration_for("left") is None
        panel.stop()
