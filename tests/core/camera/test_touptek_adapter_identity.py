"""Tests for TouptekCameraAdapter's device-identity handling — issue
#40: a live report that selecting "G3M678M" as the Guide camera
connected "GPCMOS02000" instead. `_open_device()`'s full body (the
Open()/SerialNumber() call sequence) is `# pragma: no cover` in source
and had NO test exercising it end to end before this file — only the
pure `_select_device()` matching logic (test_touptek_adapter_no_hardware.py)
was covered. This fakes the SDK module itself (EnumV2()/Toupcam.Open()/
SerialNumber()) to close that gap and to cover the new reconnect-drift
safety net: if the same camera_id later reports a different serial
number, that's refused rather than silently accepted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from astrotool_core.camera import touptek_adapter as adapter_module
from astrotool_core.camera.touptek_adapter import TouptekCameraAdapter


class _FakeModel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.preview = 1  # _is_real_camera() requires a truthy preview/still mode
        self.still = 1
        self.flag = 0
        self.res = [SimpleNamespace(width=640, height=480)]


class _FakeDevice:
    def __init__(self, id_: str, displayname: str) -> None:
        self.id = id_
        self.displayname = displayname
        self.model = _FakeModel(displayname)


class _FakeCam:
    def __init__(self, serial: str) -> None:
        self._serial = serial

    def SerialNumber(self) -> str:
        return self._serial

    def StartPullModeWithCallback(self, callback: Any, ctx: Any) -> None:  # noqa: ANN401
        pass


class _FakeToupcamClass:
    """Stand-in for the SDK's `Toupcam` class — `EnumV2()`/`Open()` as
    plain callables, matching how touptek_adapter.py calls
    `tc.Toupcam.EnumV2()`/`tc.Toupcam.Open(device.id)`."""

    def __init__(self, devices: list[_FakeDevice], serials: dict[str, str]) -> None:
        self._devices = devices
        self._serials = serials

    def EnumV2(self) -> list[_FakeDevice]:
        return self._devices

    def Open(self, device_id: str) -> _FakeCam | None:
        serial = self._serials.get(device_id)
        return None if serial is None else _FakeCam(serial)


class _FakeToupcamModule:
    def __init__(self, devices: list[_FakeDevice], serials: dict[str, str]) -> None:
        self.Toupcam = _FakeToupcamClass(devices, serials)


_DEVICE_A = _FakeDevice("id-g3m678m", "G3M678M")
_DEVICE_B = _FakeDevice("id-gpcmos", "GPCMOS02000")


def _two_device_module(*, serial_a: str = "SN-A", serial_b: str = "SN-B") -> _FakeToupcamModule:
    return _FakeToupcamModule(
        [_DEVICE_A, _DEVICE_B], {_DEVICE_A.id: serial_a, _DEVICE_B.id: serial_b}
    )


def _fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both `_enum_devices_cache` and `_known_serials` are module-level,
    process-lifetime state — reset both so each test starts from a clean
    slate regardless of test order."""
    monkeypatch.setattr(adapter_module, "_enum_devices_cache", None)
    monkeypatch.setattr(adapter_module, "_known_serials", {})


def _reset_enum_cache_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulates a fresh enumeration pass within the same test (e.g. a
    reconnect) WITHOUT wiping remembered serials — that persistence is
    exactly what the drift check relies on."""
    monkeypatch.setattr(adapter_module, "_enum_devices_cache", None)


class TestConnectByCameraId:
    def test_connecting_by_camera_id_opens_the_matching_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_state(monkeypatch)
        tc = _two_device_module()
        adapter = TouptekCameraAdapter(camera_id=_DEVICE_A.id)

        adapter._open_device(tc)

        descriptor = adapter.get_descriptor()
        assert descriptor.serial_number == "SN-A"
        assert descriptor.logical_name == "G3M678M"

    def test_connecting_by_the_other_camera_id_opens_that_device(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_state(monkeypatch)
        tc = _two_device_module()
        adapter = TouptekCameraAdapter(camera_id=_DEVICE_B.id)

        adapter._open_device(tc)

        descriptor = adapter.get_descriptor()
        assert descriptor.serial_number == "SN-B"
        assert descriptor.logical_name == "GPCMOS02000"


class TestReconnectDriftDetection:
    def test_reconnecting_with_an_unchanged_serial_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_state(monkeypatch)
        tc = _two_device_module()
        first = TouptekCameraAdapter(camera_id=_DEVICE_A.id)
        first._open_device(tc)

        _reset_enum_cache_only(monkeypatch)
        second = TouptekCameraAdapter(camera_id=_DEVICE_A.id)
        second._open_device(tc)  # same serial as before -- fine

        assert second.get_descriptor().serial_number == "SN-A"

    def test_a_serial_change_on_reconnect_is_refused_not_silently_accepted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_state(monkeypatch)
        first_tc = _two_device_module(serial_a="SN-A")
        first = TouptekCameraAdapter(camera_id=_DEVICE_A.id)
        first._open_device(first_tc)

        # The SAME camera_id now reports a DIFFERENT serial -- as if the
        # physical device behind that id changed.
        _reset_enum_cache_only(monkeypatch)
        drifted_tc = _two_device_module(serial_a="SN-DRIFTED")
        second = TouptekCameraAdapter(camera_id=_DEVICE_A.id)

        with pytest.raises(ConnectionError, match="previously reported serial"):
            second._open_device(drifted_tc)

    def test_different_camera_ids_are_tracked_independently(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fresh_state(monkeypatch)
        tc = _two_device_module()
        a = TouptekCameraAdapter(camera_id=_DEVICE_A.id)
        a._open_device(tc)

        _reset_enum_cache_only(monkeypatch)
        b = TouptekCameraAdapter(camera_id=_DEVICE_B.id)
        b._open_device(tc)  # a different id/serial pair -- not a drift

        assert b.get_descriptor().serial_number == "SN-B"
