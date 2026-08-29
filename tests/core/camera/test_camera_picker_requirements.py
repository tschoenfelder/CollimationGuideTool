"""Below-UI requirement tests for the camera picker (no Qt involved).

Requirement 1 (this file's first and, so far, only requirement):
"the UI must receive all cameras, including the demo camera and every
connected ToupTek device." `build_camera_choices()` is the pure function
both apps' MainWindows use to populate their Camera combo box (see
`camera_selection.py`) — testing it here proves the requirement at the
architectural level below the UI, independent of PySide6/QComboBox.

`TestCameraChoicesComposition` needs no hardware and runs everywhere.
`TestRealHardwareIsIncluded` additionally proves the requirement against
whatever ToupTek SDK/camera is actually present on the machine running
the suite — skipped with a clear reason when there isn't one (e.g. this
project's Windows dev environment), meaningful when run on the Pi with a
real camera attached.
"""

from __future__ import annotations

import pytest
from astrotool_core.camera import (
    DEMO_CAMERA_LABEL,
    TouptekDeviceInfo,
    build_camera_choices,
    list_devices,
)


def _toupcam_sdk_available() -> bool:
    try:
        import toupcam  # noqa: F401
    except ImportError:
        return False
    return True


class TestCameraChoicesComposition:
    def test_no_devices_still_offers_the_demo_camera(self) -> None:
        choices = build_camera_choices([])
        assert len(choices) == 1
        assert choices[0].label == DEMO_CAMERA_LABEL
        assert choices[0].device is None

    def test_demo_camera_is_always_first_regardless_of_device_count(self) -> None:
        devices = [
            TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M"),
            TouptekDeviceInfo(index=1, camera_id="dev-2", display_name="GPCMOS"),
        ]
        choices = build_camera_choices(devices)
        assert choices[0].label == DEMO_CAMERA_LABEL
        assert choices[0].device is None

    def test_every_enumerated_device_gets_its_own_choice_in_order(self) -> None:
        devices = [
            TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M"),
            TouptekDeviceInfo(index=1, camera_id="dev-2", display_name="GPCMOS"),
        ]
        choices = build_camera_choices(devices)
        assert len(choices) == 3  # demo + 2 real devices
        assert [c.device for c in choices[1:]] == devices

    def test_device_choice_label_includes_display_name_and_camera_id(self) -> None:
        device = TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide")
        choices = build_camera_choices([device])
        assert choices[1].label == "ATR585M Guide (dev-1)"

    def test_a_single_device_never_replaces_the_demo_camera(self) -> None:
        """Regression guard: the combo must never end up demo-only-or-real-only."""
        device = TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M")
        choices = build_camera_choices([device])
        labels = [c.label for c in choices]
        assert DEMO_CAMERA_LABEL in labels
        assert "ATR585M (dev-1)" in labels


class TestRealHardwareIsIncluded:
    """Proves the requirement end-to-end wherever real hardware is present.

    Run this on the Raspberry Pi (with the ToupTek SDK vendored and a
    camera attached — see issue #12) to actually validate "the UI will
    receive all cameras including the touptek and the demo camera"
    against real hardware, not just the composition logic above.
    """

    @pytest.mark.skipif(
        not _toupcam_sdk_available(), reason="toupcam SDK not installed in this environment"
    )
    def test_list_devices_finds_at_least_one_real_camera(self) -> None:
        devices = list_devices()
        assert len(devices) >= 1, (
            "toupcam SDK is installed but list_devices() found no cameras — "
            "is a ToupTek camera actually connected and powered?"
        )
        for device in devices:
            assert device.camera_id
            assert device.display_name
            assert device.index >= 0

    @pytest.mark.skipif(
        not _toupcam_sdk_available(), reason="toupcam SDK not installed in this environment"
    )
    def test_picker_offers_demo_camera_and_every_real_device_together(self) -> None:
        devices = list_devices()
        choices = build_camera_choices(devices)

        assert choices[0].label == DEMO_CAMERA_LABEL
        assert choices[0].device is None

        real_choices = choices[1:]
        assert len(real_choices) == len(devices)
        assert {c.device.camera_id for c in real_choices if c.device is not None} == {
            d.camera_id for d in devices
        }
