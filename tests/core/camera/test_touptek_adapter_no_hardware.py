"""Unit tests for TouptekCameraAdapter's pure/no-hardware-required logic:
the not-connected default-returning getters, device-selection logic, and
small pure helper functions. Everything that genuinely requires a real
ToupTek SDK/camera is marked ``# pragma: no cover`` in the source and is
instead exercised by the skipif-guarded real-hardware contract test.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from astrotool_core.camera.capabilities import ConversionGain
from astrotool_core.camera.touptek_adapter import (
    TouptekCameraAdapter,
    TouptekDeviceInfo,
    _devices_to_info,
    _normalise_camera_name,
    _opt,
    list_devices,
)


@dataclass
class _FakeDevice:
    id: str
    displayname: str
    model_name: str = "SomeModel"

    class _Model:
        def __init__(self, name: str) -> None:
            self.name = name

    @property
    def model(self) -> _FakeDevice._Model:
        return _FakeDevice._Model(self.model_name)


def test_connect_raises_connection_error_when_sdk_missing() -> None:
    adapter = TouptekCameraAdapter()
    with pytest.raises(ConnectionError):
        adapter.connect()


def test_connect_is_a_noop_when_already_connected() -> None:
    adapter = TouptekCameraAdapter()
    adapter._cam = object()  # simulate an already-open device handle
    adapter.connect()  # must not raise or touch the (unavailable) SDK


def test_capture_raises_runtime_error_when_not_connected() -> None:
    adapter = TouptekCameraAdapter()
    with pytest.raises(RuntimeError):
        adapter.capture(0.1)


def test_disconnect_is_safe_when_never_connected() -> None:
    adapter = TouptekCameraAdapter()
    adapter.disconnect()  # must not raise


def test_abort_capture_sets_the_abort_event_without_hardware() -> None:
    adapter = TouptekCameraAdapter()
    adapter.abort_capture()
    assert adapter._abort.is_set()


def test_getters_return_defaults_when_not_connected() -> None:
    adapter = TouptekCameraAdapter()
    assert adapter.get_exposure_ms() == 0.0
    assert adapter.get_gain() == 100
    assert adapter.get_black_level() == 0
    assert adapter.get_conversion_gain() == ConversionGain.LCG
    assert adapter.get_temperature() is None


def test_setters_are_safe_no_ops_when_not_connected() -> None:
    adapter = TouptekCameraAdapter()
    adapter.set_exposure_ms(500.0)  # must not raise
    adapter.set_gain(200)
    assert adapter.get_gain() == 200  # local state still updates
    adapter.set_black_level(10)
    adapter.set_conversion_gain(ConversionGain.HCG)


def test_descriptor_reports_sane_defaults_when_not_connected() -> None:
    adapter = TouptekCameraAdapter()
    descriptor = adapter.get_descriptor()
    assert descriptor.serial_number == ""
    assert descriptor.capabilities.min_gain == 100
    assert descriptor.capabilities.sensor_width_px == 0


def test_is_color_sensor_reflects_model_flag_without_hardware() -> None:
    adapter = TouptekCameraAdapter()
    assert adapter.is_color_sensor() is True  # default model_flag=0 has no MONO bit
    adapter._model_flag = 0x00000040  # _FLAG_MONO
    assert adapter.is_color_sensor() is False


class TestSelectDevice:
    def test_matches_by_camera_id_hint(self) -> None:
        adapter = TouptekCameraAdapter(camera_id="dev-2")
        devices = [
            _FakeDevice(id="dev-1", displayname="Cam1"),
            _FakeDevice(id="dev-2", displayname="Cam2"),
        ]
        index, device = adapter._select_device(devices)
        assert index == 1
        assert device is devices[1]

    def test_camera_id_hint_with_no_match_returns_none(self) -> None:
        adapter = TouptekCameraAdapter(camera_id="missing")
        devices = [_FakeDevice(id="dev-1", displayname="Cam1")]
        index, device = adapter._select_device(devices)
        assert device is None

    def test_matches_by_name_selector_substring(self) -> None:
        adapter = TouptekCameraAdapter(name="g3m678m")
        devices = [
            _FakeDevice(id="dev-1", displayname="ATR585M Guide", model_name="ATR585M"),
            _FakeDevice(id="dev-2", displayname="Main Cam", model_name="G3M678M"),
        ]
        index, device = adapter._select_device(devices)
        assert index == 1
        assert device is devices[1]

    def test_name_selector_with_no_match_returns_none(self) -> None:
        adapter = TouptekCameraAdapter(name="nonexistent")
        devices = [_FakeDevice(id="dev-1", displayname="Cam1")]
        index, device = adapter._select_device(devices)
        assert device is None

    def test_falls_back_to_positional_index_with_no_selector(self) -> None:
        adapter = TouptekCameraAdapter(index=1)
        devices = [
            _FakeDevice(id="dev-1", displayname="Cam1"),
            _FakeDevice(id="dev-2", displayname="Cam2"),
        ]
        index, device = adapter._select_device(devices)
        assert index == 1
        assert device is devices[1]

    def test_index_out_of_range_returns_none(self) -> None:
        adapter = TouptekCameraAdapter(index=5)
        devices = [_FakeDevice(id="dev-1", displayname="Cam1")]
        index, device = adapter._select_device(devices)
        assert device is None


class TestListDevices:
    def test_devices_to_info_maps_enumerated_devices(self) -> None:
        devices = [
            _FakeDevice(id="dev-1", displayname="ATR585M Guide"),
            _FakeDevice(id="dev-2", displayname="", model_name="G3M678M"),
        ]
        infos = _devices_to_info(devices)
        assert infos == [
            TouptekDeviceInfo(index=0, camera_id="dev-1", display_name="ATR585M Guide"),
            # falls back to the model name when displayname is blank
            TouptekDeviceInfo(index=1, camera_id="dev-2", display_name="G3M678M"),
        ]

    def test_devices_to_info_empty_list_returns_empty_list(self) -> None:
        assert _devices_to_info([]) == []

    def test_list_devices_returns_empty_list_when_sdk_missing(self) -> None:
        # No `toupcam` package is installed in this dev/CI environment (no
        # vendored SDK wheel yet — see pyproject.toml's touptek extra) —
        # exactly the environment list_devices() must degrade gracefully in.
        assert list_devices() == []


def test_normalise_camera_name_strips_spaces_and_underscores() -> None:
    assert _normalise_camera_name("G3M 678_M") == "G3M678M"


def test_opt_falls_back_when_attribute_missing() -> None:
    class _Module:
        TOUPCAM_OPTION_RAW = 99

    assert _opt(_Module(), "TOUPCAM_OPTION_RAW", 4) == 99
    assert _opt(_Module(), "TOUPCAM_OPTION_MISSING", 4) == 4
