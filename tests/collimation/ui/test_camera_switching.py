"""Issue #45: switching the Main (or Guide) camera must really switch it.

Real incident (diagnostic e10dd9ff): switching cameras left the previous
camera's SDK handle open, so re-opening a device that had been swapped out
failed with ERROR_BUSY (0x800700AA) -- and the SDK's HRESULTException escaped
the connect handler as an unhandled incident. The fake below models the real
constraint: ONE open handle per physical device."""

from __future__ import annotations

import numpy as np
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.camera.touptek_adapter import TouptekDeviceInfo
from astrotool_core.frames.frame import Frame
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from collimation_tool.ui.main_window import MainWindow

_A = TouptekDeviceInfo(index=0, camera_id="id-a", display_name="G3M678M")
_B = TouptekDeviceInfo(index=1, camera_id="id-b", display_name="ATR585M")


class _OneHandleCamera(FakeTouptekCamera):
    """A device that can only be opened once until it is disconnected."""

    def __init__(self, registry: dict[str, str], camera_id: str, name: str, serial: str) -> None:
        super().__init__(logical_name=name, serial_number=serial)
        self._registry = registry
        self._camera_id = camera_id
        self.open = False
        self.connect_error: Exception | None = None

    @property
    def device_id(self) -> str:
        return self._camera_id

    def connect(self) -> None:
        if self.open:
            return
        if self.connect_error is not None:
            raise self.connect_error
        if self._registry.get(self._camera_id):
            raise ConnectionError("device busy (0x800700AA): already open elsewhere")
        self._registry[self._camera_id] = "open"
        self.open = True

    def disconnect(self) -> None:
        if self.open:
            self._registry.pop(self._camera_id, None)
            self.open = False


class _Rig:
    def __init__(self) -> None:
        self.registry: dict[str, str] = {}
        self.cameras: dict[str, _OneHandleCamera] = {}

    def factory(self, camera_id: str) -> _OneHandleCamera:
        # A fresh adapter per Connect click, like the real factory.
        name = {"id-a": "G3M678M", "id-b": "ATR585M"}[camera_id]
        cam = _OneHandleCamera(self.registry, camera_id, name, f"SN-{camera_id}")
        self.cameras[camera_id] = cam
        return cam


def _window(rig: _Rig) -> MainWindow:
    image = np.full((60, 80), 100.0, dtype=np.float32)
    demo = ReplayCamera.from_arrays([image], cycle=True)
    return MainWindow(demo, device_lister=lambda: [_A, _B], camera_factory=rig.factory)


def _select(window: MainWindow, side: str, camera_id: str | None) -> None:
    panel = window._left_panel if side == "left" else window._right_panel
    combo = panel._camera_combo
    for i in range(combo.count()):
        data = combo.itemData(i)
        if (data is None and camera_id is None) or (
            isinstance(data, TouptekDeviceInfo) and data.camera_id == camera_id
        ):
            combo.setCurrentIndex(i)
            break
    else:
        raise AssertionError(f"{camera_id} not offered on {side}")
    panel._on_connect_camera()


class TestSwitchLifecycle:
    def test_switch_a_to_b_uses_b_and_releases_a(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        first = window._left_panel._camera
        assert first is rig.cameras["id-a"]

        _select(window, "left", "id-b")

        assert window._left_panel._camera is rig.cameras["id-b"]
        assert "ATR585M" in window._left_panel._camera_status_label.text()
        assert rig.cameras["id-a"].open is False  # old handle released
        assert rig.registry == {"id-b": "open"}

    def test_switching_back_to_the_first_camera_works(self, qapp: object) -> None:
        """The e10dd9ff regression: re-opening a swapped-out camera was BUSY."""
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        _select(window, "left", "id-b")
        _select(window, "left", "id-a")

        assert window._left_panel._camera is rig.cameras["id-a"]
        assert "G3M678M" in window._left_panel._camera_status_label.text()
        assert "failed" not in window._left_panel._camera_status_label.text().lower()

    def test_reconnecting_the_same_camera_does_not_hit_busy(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        _select(window, "left", "id-a")

        assert "failed" not in window._left_panel._camera_status_label.text().lower()
        assert window._left_panel._camera.open is True  # type: ignore[attr-defined]

    def test_choosing_demo_releases_the_real_camera(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        _select(window, "left", None)

        assert rig.registry == {}
        assert window._left_panel._camera is window._left_panel._demo_camera

    def test_main_and_guide_can_swap_roles(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")  # Main = A
        _select(window, "right", "id-b")  # Guide = B
        # Move A to Guide and B to Main: free both first via demo, as the UI forces.
        _select(window, "left", None)
        _select(window, "right", None)
        _select(window, "left", "id-b")
        _select(window, "right", "id-a")

        assert window._left_panel._camera is rig.cameras["id-b"]
        assert window._right_panel._camera is rig.cameras["id-a"]
        assert "failed" not in window._left_panel._camera_status_label.text().lower()
        assert "failed" not in window._right_panel._camera_status_label.text().lower()


class TestFailedSwitch:
    def test_a_busy_device_keeps_the_previous_camera_and_says_so(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        rig.registry["id-b"] = "open"  # someone else holds B

        _select(window, "left", "id-b")

        panel = window._left_panel
        assert panel._camera is rig.cameras["id-a"]
        assert rig.cameras["id-a"].open is True  # previous camera restored
        assert "failed" in panel._camera_status_label.text().lower()
        # combo re-synced to the camera that is actually active
        current = panel._camera_combo.currentData()
        assert isinstance(current, TouptekDeviceInfo) and current.camera_id == "id-a"

    def test_a_non_connectionerror_sdk_exception_is_handled_not_raised(self, qapp: object) -> None:
        class HRESULTException(Exception):  # stand-in for toupcam.HRESULTException
            pass

        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        original = rig.factory

        def factory(camera_id: str) -> _OneHandleCamera:
            cam = original(camera_id)
            if camera_id == "id-b":
                cam.connect_error = HRESULTException("-2147024726")
            return cam

        window._left_panel._camera_factory = factory
        _select(window, "left", "id-b")  # must not raise

        assert "failed" in window._left_panel._camera_status_label.text().lower()
        assert window._left_panel._camera is rig.cameras["id-a"]

    def test_a_failed_switch_does_not_persist_the_new_selection(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        rig.registry["id-b"] = "open"
        saved: list[str | None] = []
        window._left_panel.settings_changed.connect(
            lambda: saved.append(
                window._left_panel._connected_device.camera_id
                if window._left_panel._connected_device
                else None
            )
        )

        _select(window, "left", "id-b")

        assert all(camera_id == "id-a" for camera_id in saved)

    def test_a_device_reporting_a_different_id_is_rejected(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        original = rig.factory

        def factory(camera_id: str) -> _OneHandleCamera:
            cam = original(camera_id)
            if camera_id == "id-b":
                cam._camera_id = "id-a-impostor"  # opened a different device
            return cam

        window._left_panel._camera_factory = factory
        _select(window, "left", "id-b")

        assert window._left_panel._camera is rig.cameras["id-a"]
        assert "failed" in window._left_panel._camera_status_label.text().lower()


class TestNoStaleState:
    def test_frames_and_analysis_are_cleared_on_a_successful_switch(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        panel = window._left_panel
        panel._recent_frames.append(
            Frame(pixels=np.zeros((4, 4), dtype=np.float32), header={}, exposure_seconds=0.1)
        )
        panel._last_sequence = 42

        _select(window, "left", "id-b")

        assert panel.recent_frames() == []
        assert panel._last_sequence == 0
        assert panel._last_result is None
        assert panel.latest_mono_frame() is None

    def test_main_dependent_callables_read_the_new_camera(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        _select(window, "left", "id-b")
        # exposure control (autofocus) talks to whichever camera is active now
        control_get = window._focuser_panel._exposure_control
        assert control_get is not None
        window._left_panel.apply_exposure_gain(12.0, 200)
        qapp.processEvents()  # type: ignore[attr-defined]
        assert rig.cameras["id-b"].get_gain() == 200


class TestDiagnostics:
    def test_diagnostics_report_requested_actual_and_previous_identity(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        _select(window, "left", "id-b")

        left = window._diagnostic_context()["left"]

        assert left["camera_switch"]["requested_camera_id"] == "id-b"
        assert left["camera_switch"]["actual_camera_id"] == "id-b"
        assert left["camera_switch"]["actual_serial"] == "SN-id-b"
        assert left["camera_switch"]["previous_camera_id"] == "id-a"
        assert left["camera_switch"]["last_result"] == "ok"

    def test_diagnostics_record_a_failed_switch_reason(self, qapp: object) -> None:
        rig = _Rig()
        window = _window(rig)
        _select(window, "left", "id-a")
        rig.registry["id-b"] = "open"
        _select(window, "left", "id-b")

        switch = window._diagnostic_context()["left"]["camera_switch"]

        assert switch["last_result"] == "failed"
        assert "busy" in switch["last_error"].lower()
        assert switch["requested_camera_id"] == "id-b"
        assert switch["actual_camera_id"] == "id-a"
