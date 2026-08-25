"""Single-shot capture helper — thin wrapper around CameraPort.capture()
normalizing abort/error outcomes into an AcquisitionState, shared by both
apps' single-frame flows (e.g. a manual "capture now" action).
"""

from __future__ import annotations

from dataclasses import dataclass

from astrotool_core.acquisition.acquisition_state import AcquisitionState
from astrotool_core.camera.port import CameraPort, CaptureAbortedError
from astrotool_core.frames.frame import Frame


@dataclass(frozen=True)
class CaptureResult:
    state: AcquisitionState
    frame: Frame | None
    error: str | None = None


def capture_once(camera: CameraPort, exposure_seconds: float) -> CaptureResult:
    """Capture a single frame, normalizing abort/error into a CaptureResult."""
    try:
        frame = camera.capture(exposure_seconds)
    except CaptureAbortedError:
        return CaptureResult(state=AcquisitionState.ABORTED, frame=None)
    except Exception as exc:
        return CaptureResult(state=AcquisitionState.ERROR, frame=None, error=str(exc))
    return CaptureResult(state=AcquisitionState.IDLE, frame=frame)
