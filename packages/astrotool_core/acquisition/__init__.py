"""Single-capture and continuous-stream orchestration on top of a CameraPort."""

from astrotool_core.acquisition.acquisition_state import AcquisitionState
from astrotool_core.acquisition.auto_exposure import (
    AutoExposureConfig,
    AutoExposureResult,
    compute_auto_exposure,
)
from astrotool_core.acquisition.single_capture import CaptureResult, capture_once
from astrotool_core.acquisition.stream_controller import (
    FrameMailbox,
    MailboxFrame,
    StreamController,
)

__all__ = [
    "AcquisitionState",
    "AutoExposureConfig",
    "AutoExposureResult",
    "CaptureResult",
    "FrameMailbox",
    "MailboxFrame",
    "StreamController",
    "capture_once",
    "compute_auto_exposure",
]
