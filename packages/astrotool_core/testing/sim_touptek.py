"""Scenario simulator for the ToupTek camera SDK (`toupcam`) behind the
production `TouptekCameraAdapter` (issue #51).

The simulated module is installed as `sys.modules["toupcam"]`, so the
adapter's real `connect()` -> `_open_device()` -> `_basic_configure()` ->
`_prepare_capture_mode()` -> `capture()` -> `_capture_raw()` ->
`_pull_pixels()` path runs unchanged against it. What it models:

- the SDK's **real constants** (`TOUPCAM_FLAG_*`, `TOUPCAM_OPTION_*`,
  `TOUPCAM_EVENT_*`), copied from the vendored SDK wrapper
  `resources/touptek/toupcam.py` and exported both here and as attributes of
  the simulated module (the adapter looks option ids up by name there);
  `tests/core/camera/test_touptek_adapter_simulated.py` asserts every copied
  value equals that file. The simulator follows the SDK, never the adapter
  (the adapter's own fallback table is checked against the same file since
  S6.5a);
- a **capability matrix per representative camera model** (`CAMERA_MODELS`):
  SDK flag bits (mono, USB3, TEC, conversion gain, black level, RAW16), sensor
  size, raw FourCC + ADC bit depth, gain/exposure ranges, whether a
  temperature sensor exists. Capabilities come from this project's field
  reports (see each model); the USB-generation bits are from the model
  family names, not verified on the rig. Two synthetic models
  (`SYNTH-COLOR-USB3`, `SYNTH-MONO-USB2`) cover flag combinations the rig's
  cameras happen not to have;
- **E_NOTIMPL** (HRESULT 0x80004001) for every SDK call the model does not
  support -- raised only when the call is actually made, and recorded in
  `SimulatedToupcam.notimpl_calls`, so a test can prove the adapter never
  makes doomed calls (531a952, 4730c55);
- **exposure/gain metadata** (`put_ExpoTime`/`get_ExpoTime` in microseconds,
  analog gain) and **frame delivery timing**: a software `Trigger` exposes
  for the current exposure time *on the injected clock* (a long exposure
  costs no real time), renders the frame from a `scene(start, end)` callback
  and fires the SDK event callback; `exposures` records each exposure's
  start/end on that clock;
- trigger failure / disconnect / error events.

Only the vendor surface the adapter uses is modeled; this is not a vendor SDK
emulation.

`install_simulated_toupcam(patcher, sdk)` installs the module, resets
the adapter's process-wide enumeration/serial caches, and routes the
adapter's one vendor settle sleep (0.2 s in `_prepare_capture_mode`,
allowlisted in the sleep guard) to the clock -- recorded in `clock.sleeps`
instead of slept. That is a test-side seam on the adapter's module-level
`time` name (dependency: the adapter has no clock injection).
"""

from __future__ import annotations

import ctypes
import sys
import time as _real_time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from astrotool_core.camera import touptek_adapter
from astrotool_core.timing import Clock

#: Real SDK constants, copied from resources/touptek/toupcam.py (asserted
#: equal to that file by tests/core/camera/test_touptek_adapter_simulated.py).
SDK_CONSTANTS: dict[str, int] = {
    "TOUPCAM_FLAG_MONO": 0x00000010,
    "TOUPCAM_FLAG_USB30": 0x00000040,
    "TOUPCAM_FLAG_TEC": 0x00000080,
    "TOUPCAM_FLAG_RAW16": 0x00008000,
    "TOUPCAM_FLAG_TEC_ONOFF": 0x00020000,
    "TOUPCAM_FLAG_BLACKLEVEL": 0x00400000,
    "TOUPCAM_FLAG_CG": 0x04000000,
    "TOUPCAM_FLAG_CGHDR": 0x0000000800000000,
    "TOUPCAM_EVENT_IMAGE": 0x0004,
    "TOUPCAM_EVENT_STILLIMAGE": 0x0005,
    "TOUPCAM_EVENT_TRIGGERFAIL": 0x0007,
    "TOUPCAM_EVENT_ERROR": 0x0080,
    "TOUPCAM_EVENT_DISCONNECTED": 0x0081,
    "TOUPCAM_OPTION_NOFRAME_TIMEOUT": 0x01,
    "TOUPCAM_OPTION_RAW": 0x04,
    "TOUPCAM_OPTION_BITDEPTH": 0x06,
    "TOUPCAM_OPTION_TEC": 0x08,
    "TOUPCAM_OPTION_TRIGGER": 0x0B,
    "TOUPCAM_OPTION_RGB": 0x0C,
    "TOUPCAM_OPTION_TECTARGET": 0x0F,
    "TOUPCAM_OPTION_BLACKLEVEL": 0x15,
    "TOUPCAM_OPTION_CG": 0x19,
    "TOUPCAM_OPTION_FLUSH": 0x3D,
    "TOUPCAM_OPTION_AUTOEXPO_TRIGGER": 0x51,
    "TOUPCAM_OPTION_TECTARGET_RANGE": 0x6D,
}

FLAG_MONO = SDK_CONSTANTS["TOUPCAM_FLAG_MONO"]
FLAG_USB30 = SDK_CONSTANTS["TOUPCAM_FLAG_USB30"]
FLAG_TEC = SDK_CONSTANTS["TOUPCAM_FLAG_TEC"]
FLAG_RAW16 = SDK_CONSTANTS["TOUPCAM_FLAG_RAW16"]
FLAG_TEC_ONOFF = SDK_CONSTANTS["TOUPCAM_FLAG_TEC_ONOFF"]
FLAG_BLACKLEVEL = SDK_CONSTANTS["TOUPCAM_FLAG_BLACKLEVEL"]
FLAG_CG = SDK_CONSTANTS["TOUPCAM_FLAG_CG"]

OPTION_TEC = SDK_CONSTANTS["TOUPCAM_OPTION_TEC"]
OPTION_TECTARGET = SDK_CONSTANTS["TOUPCAM_OPTION_TECTARGET"]
OPTION_BLACKLEVEL = SDK_CONSTANTS["TOUPCAM_OPTION_BLACKLEVEL"]
OPTION_CG = SDK_CONSTANTS["TOUPCAM_OPTION_CG"]

EVENT_IMAGE = SDK_CONSTANTS["TOUPCAM_EVENT_IMAGE"]
EVENT_TRIGGER_FAIL = SDK_CONSTANTS["TOUPCAM_EVENT_TRIGGERFAIL"]
EVENT_ERROR = SDK_CONSTANTS["TOUPCAM_EVENT_ERROR"]
EVENT_DISCONNECTED = SDK_CONSTANTS["TOUPCAM_EVENT_DISCONNECTED"]

E_NOTIMPL = -2147467263  # 0x80004001 as the SDK's signed HRESULT
E_ACCESSDENIED = -2147024891  # 0x80070005


def _fourcc(code: str) -> int:
    return ord(code[0]) | (ord(code[1]) << 8) | (ord(code[2]) << 16) | (ord(code[3]) << 24)


class HRESULTException(Exception):
    """Shape of `toupcam.HRESULTException`: the signed HRESULT in `.hr`."""

    def __init__(self, hr: int) -> None:
        super().__init__(hr)
        self.hr = hr


@dataclass(frozen=True)
class CameraModel:
    name: str
    flag: int
    width: int
    height: int
    raw_fourcc: str
    adc_bits: int
    has_temperature_sensor: bool
    gain_range: tuple[int, int, int] = (100, 3200, 100)
    #: Exposure range in microseconds (min, max, default).
    exposure_range_us: tuple[int, int, int] = (100, 3_600_000_000, 2_000_000)
    tec_target_range: tuple[int, int] = (-500, 400)  # 0.1 C units

    @property
    def mono(self) -> bool:
        return bool(self.flag & FLAG_MONO)

    @property
    def has_tec(self) -> bool:
        return bool(self.flag & (FLAG_TEC | FLAG_TEC_ONOFF))


#: Representative models (scaled-down sensors keep frames small; the adapter
#: takes the size from `get_Size`). USB bits: G3*/ATR* are USB3 families,
#: GPCMOS02000KPA a USB2 guide camera -- from the model families, unverified.
CAMERA_MODELS: dict[str, CameraModel] = {
    # Main camera: mono 'YYYY', 16-bit, no TEC (531a952's report).
    "G3M678M": CameraModel(
        "G3M678M", FLAG_MONO | FLAG_USB30 | FLAG_RAW16 | FLAG_BLACKLEVEL, 96, 64, "YYYY", 16, True
    ),
    # Guide camera: color, ~12-bit codes delivered unpadded, NO temperature
    # sensor -> get_Temperature is E_NOTIMPL (4730c55's report).
    "GPCMOS02000KPA": CameraModel("GPCMOS02000KPA", 0, 96, 64, "RGGB", 12, False),
    # Cooled mono: TEC with on/off, 16-bit, conversion gain (30bd33d).
    "ATR585M": CameraModel(
        "ATR585M",
        FLAG_MONO | FLAG_USB30 | FLAG_RAW16 | FLAG_TEC | FLAG_TEC_ONOFF | FLAG_CG | FLAG_BLACKLEVEL,
        96,
        64,
        "YYYY",
        16,
        True,
    ),
    # Synthetic: flag combinations the rig's cameras happen not to have.
    "SYNTH-COLOR-USB3": CameraModel("SYNTH-COLOR-USB3", FLAG_USB30, 96, 64, "RGGB", 12, True),
    "SYNTH-MONO-USB2": CameraModel("SYNTH-MONO-USB2", FLAG_MONO, 96, 64, "YYYY", 12, True),
}


@dataclass(frozen=True)
class ExposureRecord:
    start: float
    end: float
    exposure_s: float
    gain: int


#: Renders one exposure: (exposure start, exposure end) on the clock -> 2-D
#: array of ADC codes (any numeric dtype; clipped to the model's ADC range).
Scene = Callable[[float, float], np.ndarray]


def flat_scene(level: float = 500.0) -> Scene:
    return lambda _start, _end: np.full((1, 1), level)


@dataclass
class _Res:
    width: int
    height: int


@dataclass
class _SdkModel:
    name: str
    flag: int
    preview: int
    still: int
    res: list[_Res]


@dataclass
class SimulatedDevice:
    """An `EnumV2()` entry (`id`, `displayname`, `model`)."""

    id: str
    displayname: str
    model: _SdkModel
    camera: CameraModel
    serial: str


class ToupcamFrameInfoV2:
    def __init__(self) -> None:
        self.width = 0
        self.height = 0
        self.flag = 0
        self.seq = 0
        self.timestamp = 0


@dataclass
class SimulatedToupcam:
    """One open camera handle (what `Toupcam.Open()` returns)."""

    device: SimulatedDevice
    clock: Clock
    scene: Scene
    #: "image" (default), "trigger_fail", "disconnected", "error", "none".
    trigger_outcome: str = "image"
    exposure_us: int = 2_000_000
    gain: int = 100
    temperature_c10: int = 125
    options: dict[int, int] = field(default_factory=dict)
    notimpl_calls: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    exposures: list[ExposureRecord] = field(default_factory=list)
    closed: bool = False
    _callback: Callable[[int, Any], None] | None = None
    _ctx: Any = None
    _frame: np.ndarray | None = None

    @property
    def model(self) -> CameraModel:
        return self.device.camera

    def _notimpl(self, name: str) -> HRESULTException:
        self.notimpl_calls.append(name)
        return HRESULTException(E_NOTIMPL)

    def _supported_option(self, option: int) -> bool:
        if option in (OPTION_TEC, OPTION_TECTARGET):
            return self.model.has_tec
        if option == OPTION_CG:
            return bool(self.model.flag & FLAG_CG)
        if option == OPTION_BLACKLEVEL:
            return bool(self.model.flag & FLAG_BLACKLEVEL)
        return True

    # -- identity / lifecycle -------------------------------------------------
    def SerialNumber(self) -> str:
        return self.device.serial

    def get_Size(self) -> tuple[int, int]:
        return self.model.width, self.model.height

    def Stop(self) -> None:
        self.calls.append("Stop")

    def Close(self) -> None:
        self.calls.append("Close")
        self.closed = True

    def StartPullModeWithCallback(
        self,
        callback: Callable[[int, Any], None],
        ctx: Any,  # noqa: ANN401
    ) -> None:
        self._callback, self._ctx = callback, ctx

    # -- options -----------------------------------------------------------------
    def put_Option(self, option: int, value: int) -> None:
        if not self._supported_option(option):
            raise self._notimpl(f"put_Option(0x{option:02X})")
        self.options[option] = value

    def get_Option(self, option: int) -> int:
        if not self._supported_option(option):
            raise self._notimpl(f"get_Option(0x{option:02X})")
        return self.options.get(option, 0)

    def put_AutoExpoEnable(self, value: int) -> None:
        self.options[-1] = value

    # -- exposure / gain -----------------------------------------------------------
    def get_ExpoTime(self) -> int:
        return self.exposure_us

    def put_ExpoTime(self, us: int) -> None:
        low, high, _default = self.model.exposure_range_us
        self.exposure_us = min(max(int(us), low), high)

    def get_ExpTimeRange(self) -> tuple[int, int, int]:
        return self.model.exposure_range_us

    def get_ExpoAGain(self) -> int:
        return self.gain

    def put_ExpoAGain(self, gain: int) -> None:
        low, high, _default = self.model.gain_range
        self.gain = min(max(int(gain), low), high)

    def get_ExpoAGainRange(self) -> tuple[int, int, int]:
        return self.model.gain_range

    # -- capability-gated sensor calls -------------------------------------------------
    def get_Temperature(self) -> int:
        if not self.model.has_temperature_sensor:
            raise self._notimpl("get_Temperature")
        return self.temperature_c10

    def get_TecTargetRange(self) -> tuple[int, int]:
        if not self.model.has_tec:
            raise self._notimpl("get_TecTargetRange")
        return self.model.tec_target_range

    def get_RawFormat(self) -> tuple[int, int]:
        return _fourcc(self.model.raw_fourcc), self.model.adc_bits

    # -- capture -------------------------------------------------------------------------
    def Trigger(self, count: int) -> None:
        """Software trigger: exposes on the clock, then fires the event."""
        self.calls.append(f"Trigger({count})")
        if count == 0 or self._callback is None:
            return
        exposure_s = self.exposure_us / 1_000_000.0
        start = self.clock.monotonic()
        self.clock.sleep(exposure_s)
        end = self.clock.monotonic()
        self.exposures.append(ExposureRecord(start, end, exposure_s, self.gain))
        outcome = self.trigger_outcome
        if outcome == "none":
            return  # no event ever arrives: the adapter's own timeout decides
        if outcome == "image":
            self._frame = self._render(start, end)
            self._callback(EVENT_IMAGE, self._ctx)
        else:
            event = {
                "trigger_fail": EVENT_TRIGGER_FAIL,
                "disconnected": EVENT_DISCONNECTED,
                "error": EVENT_ERROR,
            }[outcome]
            self._callback(event, self._ctx)

    def _render(self, start: float, end: float) -> np.ndarray:
        pixels = np.asarray(self.scene(start, end), dtype=np.float64)
        shape = (self.model.height, self.model.width)
        if pixels.shape != shape:
            pixels = np.broadcast_to(pixels if pixels.size == 1 else pixels[:1, :1], shape)
        ceiling = (1 << self.model.adc_bits) - 1
        return np.clip(np.rint(pixels), 0, ceiling).astype(np.uint16)

    def PullImageWithRowPitchV2(
        self,
        buffer: Any,  # noqa: ANN401 -- a ctypes string buffer, as the SDK takes
        bits: int,
        _row_pitch: int,
        info: ToupcamFrameInfoV2,
    ) -> None:
        if self._frame is None:
            raise HRESULTException(E_ACCESSDENIED)
        frame = self._frame if bits > 8 else (self._frame >> 8).astype(np.uint8)
        data = np.ascontiguousarray(frame).tobytes()
        ctypes.memmove(buffer, data, len(data))
        info.width, info.height = self.model.width, self.model.height


class _ToupcamClass:
    def __init__(self, sdk: SimulatedToupcamSdk) -> None:
        self._sdk = sdk

    def EnumV2(self) -> list[SimulatedDevice]:
        self._sdk.enum_calls += 1
        return list(self._sdk.devices)

    def Open(self, device_id: str) -> SimulatedToupcam | None:
        return self._sdk.open(device_id)


class SimulatedToupcamSdk:
    """The `toupcam` module: `Toupcam.EnumV2/Open`, `ToupcamFrameInfoV2`,
    `HRESULTException`. `handles` keeps every opened camera, newest last."""

    ToupcamFrameInfoV2 = ToupcamFrameInfoV2
    HRESULTException = HRESULTException

    def __init__(self, *, clock: Clock) -> None:
        for name, value in SDK_CONSTANTS.items():  # module-level names, like toupcam.py
            setattr(self, name, value)
        self.clock = clock
        self.devices: list[SimulatedDevice] = []
        self.handles: list[SimulatedToupcam] = []
        self.scenes: dict[str, Scene] = {}
        self.enum_calls = 0
        self.Toupcam = _ToupcamClass(self)

    def add_camera(
        self,
        model: CameraModel | str,
        *,
        device_id: str | None = None,
        serial: str | None = None,
        scene: Scene | None = None,
    ) -> SimulatedDevice:
        camera = CAMERA_MODELS[model] if isinstance(model, str) else model
        device = SimulatedDevice(
            id=device_id or f"sim-{camera.name}-{len(self.devices)}",
            displayname=camera.name,
            model=_SdkModel(camera.name, camera.flag, 1, 1, [_Res(camera.width, camera.height)]),
            camera=camera,
            serial=serial or f"SN-{camera.name}-{len(self.devices)}",
        )
        self.devices.append(device)
        self.scenes[device.id] = scene or flat_scene()
        return device

    def open(self, device_id: str) -> SimulatedToupcam | None:
        for device in self.devices:
            if device.id == device_id:
                handle = SimulatedToupcam(device, self.clock, self.scenes[device_id])
                self.handles.append(handle)
                return handle
        return None


class _Patcher(Protocol):
    """The subset of `pytest.MonkeyPatch` the installer needs."""

    def setattr(self, target: Any, name: str, value: Any) -> None: ...  # noqa: ANN401

    def setitem(self, dic: Any, name: Any, value: Any) -> None: ...  # noqa: ANN401


class _ClockTime:
    """Stands in for the adapter module's `time`: `sleep` on the clock (the
    vendor settle wait), real `monotonic` (the adapter's frame-wait loop
    measures real time; the simulated event fires before it waits)."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def sleep(self, seconds: float) -> None:
        self._clock.sleep(seconds)

    def monotonic(self) -> float:
        return _real_time.monotonic()


def install_simulated_toupcam(patcher: _Patcher, sdk: SimulatedToupcamSdk) -> None:
    patcher.setitem(sys.modules, "toupcam", sdk)
    patcher.setattr(touptek_adapter, "_enum_devices_cache", None)
    patcher.setattr(touptek_adapter, "_known_serials", {})
    patcher.setattr(touptek_adapter, "time", _ClockTime(sdk.clock))
