"""Camera port, capabilities, and adapters (ToupTek, replay, fake).

Public API only — do not import adapter internals from outside this
package.
"""

from astrotool_core.camera.camera_selection import (
    DEMO_CAMERA_LABEL,
    CameraChoice,
    build_camera_choices,
)
from astrotool_core.camera.capabilities import (
    CameraCapabilities,
    CameraDescriptor,
    ConversionGain,
)
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.port import CameraPort, CaptureAbortedError
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.camera.touptek_adapter import (
    TouptekCameraAdapter,
    TouptekDeviceInfo,
    list_devices,
)
from astrotool_core.frames import Frame

__all__ = [
    "DEMO_CAMERA_LABEL",
    "CameraCapabilities",
    "CameraChoice",
    "CameraDescriptor",
    "CameraPort",
    "CaptureAbortedError",
    "ConversionGain",
    "FakeCamera",
    "Frame",
    "ReplayCamera",
    "TouptekCameraAdapter",
    "TouptekDeviceInfo",
    "build_camera_choices",
    "list_devices",
]
