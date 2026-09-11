"""Single-capture and continuous-stream orchestration on top of a CameraPort."""

from astrotool_core.acquisition.acquisition_state import AcquisitionState
from astrotool_core.acquisition.auto_exposure import (
    AutoExposureConfig,
    AutoExposureResult,
    compute_auto_exposure,
)
from astrotool_core.acquisition.image_stability import (
    StabilityCheckResult,
    StabilityStatus,
    check_image_stability,
)
from astrotool_core.acquisition.motion_aware_acquisition import (
    CommandedMovementContext,
    MotionAwareFrameResult,
    MotionAwareStatus,
    acquire_verified_frame,
    acquire_verified_frames,
)
from astrotool_core.acquisition.single_capture import CaptureResult, capture_once
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
    NextFrame,
    StableFrameWaiter,
    acquire_settled_frames,
    acquire_stable_frame,
)
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
    "CommandedMovementContext",
    "DeliveredFrame",
    "FrameAcquisitionResult",
    "FrameAcquisitionStatus",
    "FrameMailbox",
    "MailboxFrame",
    "MotionAwareFrameResult",
    "MotionAwareStatus",
    "NextFrame",
    "StabilityCheckResult",
    "StabilityStatus",
    "StableFrameWaiter",
    "StreamController",
    "acquire_settled_frames",
    "acquire_stable_frame",
    "acquire_verified_frame",
    "acquire_verified_frames",
    "capture_once",
    "check_image_stability",
    "compute_auto_exposure",
]
