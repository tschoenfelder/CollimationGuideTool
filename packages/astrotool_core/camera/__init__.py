"""Camera port, capabilities, and adapters (ToupTek, replay, fake).

Public API only — do not import adapter internals from outside this
package.
"""

from astrotool_core.camera.capabilities import (
    CameraCapabilities,
    CameraDescriptor,
    ConversionGain,
)
from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.port import CameraPort, CaptureAbortedError
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter
from astrotool_core.frames import Frame

__all__ = [
    "CameraCapabilities",
    "CameraDescriptor",
    "CameraPort",
    "CaptureAbortedError",
    "ConversionGain",
    "FakeCamera",
    "Frame",
    "ReplayCamera",
    "TouptekCameraAdapter",
]
