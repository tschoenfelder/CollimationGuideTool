"""GuideController — owns the guide-camera stream and runs the
measurement/correction loop.

Ported from smart_telescope's `services/guiding_service.py::GuidingService`,
trimmed to a single guide camera (no multi-role `GuideSourceSelector` —
see docs/porting-notes.md) and built on `astrotool_core.acquisition.
StreamController` + `astrotool_core.target.{detect_sources, select_target,
RoiTracker}` instead of `ManagedCamera` + `GuideCentroidEstimator`'s own
windowed pixel centroid (redundant with astrotool_core's detector — see
`guide_error.py`'s docstring).

In measure_only mode (default) pulses are computed but never sent to the
mount. Pass a connected `MountPort` and set `measure_only=False` to enable
closed-loop corrections via `GuideCorrectionPolicy`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from astrotool_core.acquisition.stream_controller import StreamController
from astrotool_core.camera.port import CameraPort
from astrotool_core.mount.axis_calibration import CalibrationMatrix
from astrotool_core.mount.port import MountPort
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker

from guide_tool.application.correction_policy import GuideCorrectionPolicy
from guide_tool.domain.correction_model import (
    GuideCorrectionConfig,
    WouldGuidePulse,
    compute_would_pulses,
)
from guide_tool.domain.drift_estimator import DriftEstimator
from guide_tool.domain.guide_error import GuideError, compute_guide_error
from guide_tool.domain.guiding_state import GuideSourceState, source_state_from_error

_log = logging.getLogger(__name__)


@dataclass
class GuidingStatus:
    state: str = "idle"  # idle | running | failed
    source: GuideSourceState | None = None
    latest_pulses: list[WouldGuidePulse] = field(default_factory=list)
    started_at: float | None = None
    measure_only: bool = True
    rms_px: float = 0.0


@dataclass
class GuidingFrameResult:
    """Outcome of processing one frame through the guiding business logic."""

    error: GuideError | None
    pulses: list[WouldGuidePulse]
    pulses_sent: bool
    rms_px: float


class GuideController:
    def __init__(
        self,
        camera: CameraPort,
        *,
        mount: MountPort | None = None,
        calibration: CalibrationMatrix | None = None,
        fallback_after_bad_frames: int = 5,
        max_frame_age_s: float = 2.0,
        tracker: RoiTracker | None = None,
        correction_config: GuideCorrectionConfig | None = None,
        drift_window: int = 10,
        measure_only: bool = True,
    ) -> None:
        self._camera = camera
        self._calibration = calibration
        self._fallback_after_bad_frames = fallback_after_bad_frames
        self._max_frame_age_s = max_frame_age_s
        self._tracker = tracker or RoiTracker()
        self._correction_config = correction_config or GuideCorrectionConfig()
        self._drift = DriftEstimator(window=drift_window)
        self._measure_only = measure_only
        self._correction_policy = GuideCorrectionPolicy(mount) if mount is not None else None

        self._stream: StreamController | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status = GuidingStatus(measure_only=measure_only)
        self._pulses_paused = False
        self._rebaseline_requested = threading.Event()
        self._target: tuple[float, float] | None = None
        self._acquired = False

    def start(self, *, exposure_s: float = 0.5, cadence_s: float = 0.5) -> None:
        with self._lifecycle_lock:
            with self._status_lock:
                if self._status.state == "running":
                    return

            self._stop_event.clear()
            with self._status_lock:
                self._pulses_paused = False
            self._rebaseline_requested.clear()
            self._target = None
            self._acquired = False

            self._stream = StreamController(self._camera, name="guide")
            self._stream.start_stream(exposure_s, cadence_s)

            started_at = time.monotonic()
            self._thread = threading.Thread(
                target=self._loop, args=(started_at,), daemon=True, name="guiding-loop"
            )
            self._thread.start()

            with self._status_lock:
                self._status = GuidingStatus(
                    state="running", measure_only=self._measure_only, started_at=started_at
                )
        _log.info("GuideController started measure_only=%s", self._measure_only)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_event.set()
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream = None
            if self._thread is not None:
                self._thread.join(timeout=10.0)
                self._thread = None
            with self._status_lock:
                self._status = GuidingStatus(measure_only=self._measure_only)
        _log.info("GuideController stopped")

    def status(self) -> GuidingStatus:
        with self._status_lock:
            return self._status

    def pause_pulses(self) -> None:
        """Suppress mount pulses while keeping the measurement loop running."""
        with self._status_lock:
            self._pulses_paused = True

    def resume_pulses(self) -> None:
        with self._status_lock:
            self._pulses_paused = False

    def rebaseline(self) -> None:
        """On the next accepted measurement, adopt that position as the new target."""
        self._rebaseline_requested.set()

    def _loop(self, started_at: float) -> None:
        stream = self._stream
        assert stream is not None
        last_sequence = 0
        bad_count = 0

        while not self._stop_event.is_set():
            mailbox_frame = stream.mailbox.wait_latest(after_sequence=last_sequence, timeout_s=0.1)
            hard_failure: str | None = None
            err = stream.pop_stream_error()
            if err is not None:
                hard_failure = str(err)
                _log.warning("guide stream error: %s", err)

            result: GuidingFrameResult | None = None
            latest_frame_age: float | None = None

            if mailbox_frame is None:
                bad_count += 1
            else:
                last_sequence = mailbox_frame.sequence
                frame_age = time.monotonic() - mailbox_frame.captured_at_monotonic
                latest_frame_age = frame_age
                try:
                    result = self.process_frame(mailbox_frame.frame.pixels)
                except Exception as exc:
                    _log.warning(
                        "guide measurement error seq=%s: %s", mailbox_frame.sequence, exc
                    )
                    bad_count += 1
                else:
                    error = result.error
                    if error is not None and error.accepted and frame_age <= self._max_frame_age_s:
                        bad_count = 0
                    else:
                        bad_count += 1

            state = source_state_from_error(
                result.error if result is not None else None,
                running=True,
                latest_sequence=last_sequence,
                latest_frame_age_s=latest_frame_age,
                bad_frame_count=bad_count,
                fallback_after_bad_frames=self._fallback_after_bad_frames,
                hard_failure=hard_failure,
            )

            with self._status_lock:
                self._status = GuidingStatus(
                    state="running",
                    measure_only=self._measure_only,
                    source=state,
                    latest_pulses=result.pulses if result is not None else [],
                    started_at=started_at,
                    rms_px=result.rms_px if result is not None else self._drift.rms_px(),
                )

    def process_frame(self, pixels: np.ndarray) -> GuidingFrameResult:
        """Run one frame through detect/track -> error -> pulses -> optional send.

        Synchronous business-logic entry point shared by the background
        stream loop and by below-UI acceptance tests, so both exercise the
        exact same guiding algorithm.
        """
        error = self._measure_one(pixels)

        if error is not None and error.error_magnitude_px is not None:
            self._drift.add(error.error_magnitude_px)

        pulses = self._compute_pulses(error)
        pulses_sent = self._send_pulses(pulses)

        return GuidingFrameResult(
            error=error,
            pulses=pulses,
            pulses_sent=pulses_sent,
            rms_px=self._drift.rms_px(),
        )

    def _measure_one(self, pixels: np.ndarray) -> GuideError | None:
        detection = detect_sources(pixels)

        if self._rebaseline_requested.is_set():
            self._target = None
            self._acquired = False
            self._rebaseline_requested.clear()
            self._drift.reset()

        if not self._acquired:
            target_source = select_target(detection)
            if target_source is None:
                return None
            acquire_result = self._tracker.acquire(target_source.x, target_source.y)
            self._target = (target_source.x, target_source.y)
            self._acquired = True
            return compute_guide_error(acquire_result, target=None)

        result = self._tracker.update(detection.sources)
        return compute_guide_error(result, self._target)

    def _compute_pulses(self, error: GuideError | None) -> list[WouldGuidePulse]:
        if (
            error is None
            or not error.accepted
            or error.error_x is None
            or error.error_y is None
            or self._calibration is None
        ):
            return []
        return compute_would_pulses(
            error.error_x, error.error_y, self._calibration, self._correction_config
        )

    def _send_pulses(self, pulses: list[WouldGuidePulse]) -> bool:
        with self._status_lock:
            paused = self._pulses_paused
        if self._measure_only or paused or not pulses or self._correction_policy is None:
            return False
        for pulse in pulses:
            try:
                self._correction_policy.send(pulse)
            except Exception as exc:
                _log.error("guide correction pulse failed: %s", exc)
        return True
