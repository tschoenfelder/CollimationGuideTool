"""Camera-choice composition for a UI picker.

"Which cameras should the picker offer" is pulled out as one small pure
function, kept separate from any Qt code, so it can be tested at the
architectural level below the UI — see
``tests/core/camera/test_camera_picker_requirements.py``. Both apps'
``MainWindow``s use this to populate their Camera combo box instead of
each duplicating the same demo-plus-devices logic inline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo

DEMO_CAMERA_LABEL = "Demo camera (no hardware)"


@dataclass(frozen=True)
class CameraChoice:
    """One entry a camera picker should offer.

    ``device is None`` means the always-present demo/no-hardware camera
    — the UI keeps whatever ``CameraPort`` it was constructed with for
    that entry. Otherwise ``device`` identifies a real enumerated
    ToupTek device via its ``camera_id``.
    """

    label: str
    device: TouptekDeviceInfo | None


def build_camera_choices(devices: Sequence[TouptekDeviceInfo]) -> list[CameraChoice]:
    """The demo camera, then one choice per enumerated real device.

    Requirement (both apps): the picker must always offer the demo
    camera *and* every currently-enumerated ToupTek device together —
    never just one or the other, and never dropping a device.
    """
    choices = [CameraChoice(label=DEMO_CAMERA_LABEL, device=None)]
    choices.extend(
        CameraChoice(label=f"{device.display_name} ({device.camera_id})", device=device)
        for device in devices
    )
    return choices
