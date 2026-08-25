"""run_calibration — runs Stage 4's axis calibration using a live guide camera.

New: wires `astrotool_core.mount.axis_calibration.calibrate_axes` to a
`measure` callback built from `CameraPort` + `detect_sources` + `RoiTracker`
— the same composition Stage 4's golden-master test demonstrated
(`tests/integration/test_axis_calibration_replay.py`), promoted here to
reusable application-layer orchestration rather than test-only wiring.
"""

from __future__ import annotations

from astrotool_core.camera.port import CameraPort
from astrotool_core.mount.axis_calibration import CalibrationMatrix, calibrate_axes
from astrotool_core.mount.port import MountPort
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.roi_selector import select_target
from astrotool_core.target.roi_tracker import RoiTracker


def run_calibration(
    camera: CameraPort,
    mount: MountPort,
    *,
    exposure_s: float = 0.5,
    pulse_ms: int = 500,
    lock_tolerance_px: float = 30.0,
) -> CalibrationMatrix:
    """Capture frames and pulse each axis/direction to build a CalibrationMatrix.

    The camera must already be connected and pointed at a guide star; the
    mount must already be connected.
    """
    tracker = RoiTracker(lock_tolerance_px=lock_tolerance_px)
    acquired = False

    def measure() -> tuple[float, float]:
        nonlocal acquired
        frame = camera.capture(exposure_s)
        detection = detect_sources(frame.pixels)
        if not acquired:
            target = select_target(detection)
            if target is None:
                raise RuntimeError("run_calibration: no guide star found")
            result = tracker.acquire(target.x, target.y)
            acquired = True
        else:
            result = tracker.update(detection.sources)
        if result.x is None or result.y is None:
            raise RuntimeError("run_calibration: guide star lost during calibration")
        return (result.x, result.y)

    return calibrate_axes(mount, measure=measure, pulse_ms=pulse_ms)
