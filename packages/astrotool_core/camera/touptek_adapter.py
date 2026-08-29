"""Native ToupTek camera adapter.

Ported from smart_telescope's ``adapters.touptek.managed.SmartTouptekCamera``,
trimmed to what a single-camera collimation/guide tool needs: connect,
capture, exposure/gain/black-level/conversion-gain, temperature, and
descriptor. Dropped entirely (out of scope for these tools, and each one
carries real smart_telescope-specific complexity that would need its own
characterization pass): TEC/cooling control, filter-wheel control, the
multi-"role" camera-selector/conflict-validation machinery, setup profiles,
and capture priming.

``_detect_pixel_shift`` and the ``EnumV2()``-at-most-once-per-process guard
are ported byte-for-byte — see CONTRIBUTING.md's characterization-test rule
and ``tests/core/camera/test_touptek_adapter_characterization.py``.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from dataclasses import dataclass
from functools import reduce
from math import gcd
from typing import Any

import numpy as np
from astropy.io import fits

from astrotool_core.camera.capabilities import (
    CameraCapabilities,
    CameraDescriptor,
    ConversionGain,
)
from astrotool_core.camera.port import CameraPort, CaptureAbortedError
from astrotool_core.frames.frame import Frame
from astrotool_core.frames.pixel_format import BayerPattern

_log = logging.getLogger(__name__)

_EVENT_IMAGE = 0x0004
_EVENT_STILLIMAGE = 0x0005
_EVENT_TRIGGER_FAIL = 0x0007
_EVENT_ERROR = 0x0080
_EVENT_DISCONNECTED = 0x0081

_FLAG_TEC = 0x00000080
_FLAG_TEC_ONOFF = 0x00020000
_FLAG_CG = 0x04000000
_FLAG_CGHDR = 0x0000000800000000
_FLAG_BLACKLEVEL = 0x00400000
_FLAG_MONO = 0x00000040
_FLAG_RAW16 = 0x00008000  # camera has true 16-bit ADC depth


def _fourcc(a: str, b: str, c: str, d: str) -> int:
    """Matches the SDK's MAKEFOURCC packing (little-endian; verified
    against real hardware: G3M678M's get_RawFormat() returned 0x59595959
    for 'YYYY', i.e. the first character is the least-significant byte)."""
    return ord(a) | (ord(b) << 8) | (ord(c) << 16) | (ord(d) << 24)


# get_RawFormat()'s FourCC -> BayerPattern — see toupcam.py's get_RawFormat
# docstring for the full FourCC list; only the Bayer/mono ones are relevant
# here (YUV/RGB888 raw formats aren't produced in this adapter's RAW=1
# capture mode — see _basic_configure).
_FOURCC_TO_BAYER: dict[int, BayerPattern] = {
    _fourcc("R", "G", "G", "B"): BayerPattern.RGGB,
    _fourcc("B", "G", "G", "R"): BayerPattern.BGGR,
    _fourcc("G", "R", "B", "G"): BayerPattern.GRBG,
    _fourcc("G", "B", "R", "G"): BayerPattern.GBRG,
    _fourcc("Y", "Y", "Y", "Y"): BayerPattern.MONO,
}

_OPTION_BLACKLEVEL = 0x15
_OPTION_CG = 0x19
_OPTION_BITDEPTH = 0x06
_OPTION_TRIGGER = 0x0B
_OPTION_RAW = 0x04
_OPTION_RGB = 0x16
_OPTION_FLUSH = 0x36
_OPTION_NOFRAME_TIMEOUT = 0x3F
_OPTION_AUTOEXPO_TRIGGER = 0x5A


def _detect_pixel_shift(raw: np.ndarray) -> int:
    """Detect right-shift to convert MSB-aligned sub-16-bit data to native ADC range.

    ToupTek SDK in 16-bit output mode stores data MSB-aligned:
    12-bit ADC -> x16 (shift=4), 14-bit -> x4 (shift=2), true 16-bit -> no shift.
    Returns -1 if the frame has too few distinct non-zero pixels to decide reliably.

    Uses the GCD of differences between adjacent distinct values to find the
    quantization step. This is robust to non-zero black-level offsets: with
    offset O applied to MSB-aligned data, pixel values are (ADC*16)+O. The step
    between adjacent ADC values is still 16, but (ADC*16+O) % 16 == O%16 != 0 when
    O is not a multiple of 16 — divisibility checks would wrongly return shift=2,
    creating a 4-ADU comb artifact in the histogram.
    """
    flat = raw.ravel()
    nonzero = flat[flat > 0]
    if len(nonzero) < 100:
        return -1
    distinct = np.unique(nonzero[:4096].astype(np.int32))
    if len(distinct) < 4:
        return -1  # not enough variety — retry on next frame
    diffs = np.diff(distinct)
    pos_diffs = diffs[diffs > 0]
    if len(pos_diffs) == 0:
        return -1
    step = int(reduce(gcd, pos_diffs.tolist()))
    if step >= 16:
        return 4  # 12-bit ADC
    if step >= 4:
        return 2  # 14-bit ADC
    return 0  # true 16-bit


_sdk_lifecycle_lock = threading.RLock()

# EnumV2() must be called at most once per process (ported guard — see
# smart_telescope M10-058: a real Pi crash traced to toupcam.py's
# EnumV2()->__initlib() setting ctypes _fields_ unconditionally on every
# call; under Python 3.13 a second call raises "AttributeError: _fields_ is
# final", and the next native SDK call after that segfaulted the process).
_enum_devices_cache: list[Any] | None = None


def _is_real_camera(device: Any) -> bool:  # noqa: ANN401 — untyped SDK device
    """True for an actual imaging camera, false for a non-camera accessory.

    EnumV2() enumerates ToupTek accessories (confirmed on real hardware:
    a filter wheel) alongside cameras via the same call. A real camera
    always has at least one preview or still resolution mode; an
    accessory's ``model.res`` is empty, which otherwise crashes
    ``_open_device``'s width/height fallback with an IndexError, and
    would let a filter wheel appear as a selectable "camera" in the UI.
    """
    return bool(device.model.preview) or bool(device.model.still)


def _enum_devices(tc: Any) -> list[Any]:  # noqa: ANN401 — untyped SDK module
    """Return ToupTek device enumeration, calling EnumV2() at most once per
    process, filtered to actual cameras (see ``_is_real_camera``). Callers
    must go through this instead of calling tc.Toupcam.EnumV2() directly."""
    global _enum_devices_cache
    if _enum_devices_cache is None:
        _enum_devices_cache = [d for d in tc.Toupcam.EnumV2() if _is_real_camera(d)]
    return _enum_devices_cache


@dataclass(frozen=True)
class TouptekDeviceInfo:
    """One enumerated ToupTek device, for a UI camera picker.

    ``camera_id`` is what ``TouptekCameraAdapter(camera_id=...)`` expects to
    select this same device later.
    """

    index: int
    camera_id: str
    display_name: str


def _devices_to_info(devices: Any) -> list[TouptekDeviceInfo]:  # noqa: ANN401
    return [
        TouptekDeviceInfo(
            index=i,
            camera_id=str(d.id),
            display_name=str(d.displayname or d.model.name),
        )
        for i, d in enumerate(devices)
    ]


def list_devices() -> list[TouptekDeviceInfo]:
    """Enumerate connected ToupTek cameras for a UI picker.

    Returns an empty list if the SDK isn't installed — never raises, since
    "no cameras available" is an expected, common state (e.g. this project's
    Windows dev environment has no vendored SDK wheel at all — see
    pyproject.toml's touptek extra).
    """
    try:
        import toupcam as tc
    except ImportError:
        return []
    return _devices_to_info(_enum_devices(tc))


class TouptekCameraAdapter(CameraPort):
    """CameraPort backed by one native ToupTek camera."""

    def __init__(
        self,
        *,
        index: int = 0,
        camera_id: str | None = None,
        name: str | None = None,
        bit_depth: int = 16,
        timeout_extra_s: float = 5.0,
    ) -> None:
        self._index = index
        self._camera_id_hint = camera_id
        self._name_selector = name
        self._bit_depth = 16 if bit_depth > 8 else 8
        self._timeout_extra_s = timeout_extra_s

        self._cam: Any = None
        self._tc: Any = None
        self._width = 0
        self._height = 0
        self._gain = 100
        self._model_flag = 0
        self._serial_number = ""
        self._logical_name = ""
        self._device_id = ""
        self._frame_ready = threading.Event()
        self._abort = threading.Event()
        self._capture_error: Exception | None = None
        self._capture_lock = threading.Lock()
        self._pixel_shift: int = -1  # -1=not yet detected; 0/2/4=right-shift to native range
        # See _capture_connected's docstring (issue #14) — tracks whether
        # set_exposure_ms() has ever been called, so capture()'s parameter
        # only bootstraps the very first exposure rather than permanently
        # overriding whatever was set afterward on every subsequent frame.
        self._exposure_ever_set = False

    def connect(self) -> None:
        if self._cam is not None:
            return
        try:
            import toupcam as tc
        except ImportError as exc:
            raise ConnectionError("TouptekCameraAdapter: toupcam SDK not available") from exc
        self._open_device(tc)

    def _open_device(self, tc: Any) -> None:  # noqa: ANN401  # pragma: no cover
        with _sdk_lifecycle_lock:
            devices = _enum_devices(tc)
            self._tc = tc
            self._index, device = self._select_device(devices)
            if device is None:
                listing = ", ".join(f"{i}:{d.displayname}" for i, d in enumerate(devices)) or "none"
                raise ConnectionError(
                    f"TouptekCameraAdapter: no camera matching index={self._index}, "
                    f"id={self._camera_id_hint!r}, name={self._name_selector!r}. Found: {listing}"
                )
            cam = tc.Toupcam.Open(device.id)
        if not cam:
            raise ConnectionError(f"TouptekCameraAdapter: Open() failed for {device.displayname}")
        self._cam = cam
        self._logical_name = str(device.displayname or device.model.name)
        self._device_id = str(device.id)
        self._model_flag = int(getattr(device.model, "flag", 0))
        if self._model_flag & _FLAG_RAW16:
            self._pixel_shift = 0  # true 16-bit sensor — no shift needed
        try:
            self._serial_number = cam.SerialNumber()
        except Exception:
            self._serial_number = ""
        try:
            self._width, self._height = cam.get_Size()
        except Exception:
            self._width = int(device.model.res[0].width)
            self._height = int(device.model.res[0].height)

        self._basic_configure()
        self._prepare_capture_mode()

    def disconnect(self) -> None:
        if self._cam is not None:  # pragma: no cover
            with _sdk_lifecycle_lock:
                try:
                    self._cam.Stop()
                finally:
                    self._cam.Close()
        self._cam = None
        self._tc = None
        self._exposure_ever_set = False  # a reconnect should re-bootstrap from capture()'s hint

    def abort_capture(self) -> None:
        self._abort.set()

    def capture(self, exposure_seconds: float) -> Frame:
        if self._cam is None:
            raise RuntimeError("TouptekCameraAdapter: not connected")
        return self._capture_connected(exposure_seconds)  # pragma: no cover

    def _capture_connected(self, exposure_seconds: float) -> Frame:  # pragma: no cover
        """Capture one frame.

        Deliberately does NOT unconditionally push *exposure_seconds* to the
        hardware on every call. ``StreamController._run()`` calls
        ``capture(exposure_s)`` in a loop with the *same* value it was
        started with — doing so here on every frame silently undid any live
        ``set_exposure_ms()`` call made afterward (manual spinbox edits and
        the auto-exposure feature both call it while streaming — see
        MainWindow's docstring), on the very next captured frame. Found via
        real-hardware testing (issue #14): exposure appeared "stuck" no
        matter what the UI did once streaming had started.

        *exposure_seconds* only bootstraps the very first capture after
        connect (before anything has called ``set_exposure_ms()``), so a
        bare ``connect(); capture(x)`` caller still gets what it asked for.
        After that, whatever's currently on the camera — via
        ``set_exposure_ms()`` — wins, matching how gain already works (there
        is no gain equivalent of this parameter at all).
        """
        if not self._exposure_ever_set:
            self.set_exposure_ms(exposure_seconds * 1000.0)
        if not self._capture_lock.acquire(timeout=exposure_seconds + self._timeout_extra_s + 12.0):
            raise RuntimeError("TouptekCameraAdapter: camera busy")
        try:
            actual_exposure_s = self.get_exposure_ms() / 1000.0
            raw_u16 = self._capture_raw(actual_exposure_s + self._timeout_extra_s)
            if self._pixel_shift < 0:
                self._pixel_shift = _detect_pixel_shift(raw_u16)
            shift = max(0, self._pixel_shift)
            pixels = (raw_u16 >> shift).astype(np.float32)
            hdr = fits.Header()
            hdr["CAMERA"] = self._logical_name
            hdr["CAMID"] = self._device_id
            hdr["SERIAL"] = self._serial_number
            hdr["EXPTIME"] = actual_exposure_s
            hdr["GAIN"] = self.get_gain()
            return Frame(
                pixels=pixels,
                header=hdr,
                exposure_seconds=actual_exposure_s,
                bit_depth=16 - shift,
            )
        finally:
            self._capture_lock.release()

    def get_exposure_ms(self) -> float:
        if self._cam is None:
            return 0.0
        return float(self._cam.get_ExpoTime()) / 1000.0  # pragma: no cover

    def set_exposure_ms(self, ms: float) -> None:
        self._exposure_ever_set = True
        if self._cam is not None:  # pragma: no cover
            self._cam.put_ExpoTime(max(1, int(ms * 1000)))

    def get_gain(self) -> int:
        if self._cam is not None:  # pragma: no cover
            try:
                return int(self._cam.get_ExpoAGain())
            except Exception:
                pass
        return self._gain

    def set_gain(self, gain: int) -> None:
        self._gain = max(0, int(gain))
        if self._cam is not None:  # pragma: no cover
            self._try(lambda: self._cam.put_ExpoAGain(self._gain))

    def get_black_level(self) -> int:
        if self._cam is not None:  # pragma: no cover
            value = self._get_option("TOUPCAM_OPTION_BLACKLEVEL", _OPTION_BLACKLEVEL)
            if value is not None:
                return int(value)
        return 0

    def set_black_level(self, level: int) -> None:
        if self._cam is not None:  # pragma: no cover
            self._put_option("TOUPCAM_OPTION_BLACKLEVEL", _OPTION_BLACKLEVEL, max(0, int(level)))
            self._pixel_shift = -1  # offset changes 16-bit alignment; re-detect on next frame

    def get_conversion_gain(self) -> ConversionGain:
        if self._cam is not None:  # pragma: no cover
            value = self._get_option("TOUPCAM_OPTION_CG", _OPTION_CG)
            if value is not None:
                try:
                    return ConversionGain(int(value))
                except ValueError:
                    pass
        return ConversionGain.LCG

    def set_conversion_gain(self, mode: ConversionGain) -> None:
        if self._cam is not None:  # pragma: no cover
            self._put_option("TOUPCAM_OPTION_CG", _OPTION_CG, int(mode))

    def get_temperature(self) -> float | None:
        if self._cam is None:
            return None
        value = self._try(lambda: self._cam.get_Temperature())  # pragma: no cover
        return None if value is None else round(float(value) / 10.0, 1)  # pragma: no cover

    def get_descriptor(self) -> CameraDescriptor:
        min_gain = max_gain = 100
        if self._cam is not None:  # pragma: no cover
            rng = self._try(lambda: self._cam.get_ExpoAGainRange())
            if rng:
                min_gain, max_gain = int(rng[0]), int(rng[1])
        min_exp_ms = max_exp_ms = 2000.0
        if self._cam is not None:  # pragma: no cover
            rng = self._try(lambda: self._cam.get_ExpTimeRange())
            if rng:
                min_exp_ms = float(rng[0]) / 1000.0
                max_exp_ms = float(rng[1]) / 1000.0
        capabilities = CameraCapabilities(
            min_gain=min_gain,
            max_gain=max_gain,
            min_exposure_ms=min_exp_ms,
            max_exposure_ms=max_exp_ms,
            supports_cooling=bool(self._model_flag & (_FLAG_TEC | _FLAG_TEC_ONOFF)),
            supports_hcg=bool(self._model_flag & (_FLAG_CG | _FLAG_CGHDR)),
            supports_lcg=True,
            supports_hdr=bool(self._model_flag & _FLAG_CGHDR),
            supports_black_level=bool(self._model_flag & _FLAG_BLACKLEVEL),
            bit_depth=16 - max(0, self._pixel_shift) if self._bit_depth > 8 else 8,
            pixel_size_um=0.0,
            sensor_width_px=self._width,
            sensor_height_px=self._height,
        )
        return CameraDescriptor(
            serial_number=self._serial_number,
            logical_name=self._logical_name,
            capabilities=capabilities,
        )

    def is_color_sensor(self) -> bool:
        return not bool(self._model_flag & _FLAG_MONO)

    def get_bayer_pattern(self) -> BayerPattern:
        """Query the sensor's actual Bayer layout via get_RawFormat().

        Unlike the sibling smart_telescope project's adapter (which
        hardcodes "RGGB" unconditionally), this queries the SDK directly —
        verified against real hardware: a mono camera (G3M678M) correctly
        reports fourcc 'YYYY'. Not yet verified against a real *color*
        camera (none was available/free during development — see issue
        tracker); falls back to MONO on any failure or unrecognized fourcc
        so a demosaic step is never applied to data that isn't actually
        mosaiced.
        """
        if self._cam is None or not self.is_color_sensor():  # pragma: no cover
            return BayerPattern.MONO
        try:  # pragma: no cover — requires real hardware
            fourcc, _bit_depth = self._cam.get_RawFormat()
        except Exception as exc:
            _log.warning(
                "TouptekCameraAdapter(%s): get_RawFormat() failed: %s", self._logical_name, exc
            )
            return BayerPattern.MONO
        return _FOURCC_TO_BAYER.get(int(fourcc), BayerPattern.MONO)

    def _select_device(self, devices: Any) -> tuple[int, Any | None]:  # noqa: ANN401
        if self._camera_id_hint:
            for idx, dev in enumerate(devices):
                if str(dev.id) == self._camera_id_hint:
                    return idx, dev
            return self._index, None
        if self._name_selector:
            needle = _normalise_camera_name(self._name_selector)
            for idx, dev in enumerate(devices):
                haystack = _normalise_camera_name(f"{dev.displayname} {dev.model.name}")
                if needle in haystack:
                    return idx, dev
            return self._index, None
        if len(devices) > self._index:
            return self._index, devices[self._index]
        return self._index, None

    def _get_option(self, name: str, fallback: int) -> Any:  # noqa: ANN401  # pragma: no cover
        return self._try(lambda: self._cam.get_Option(_opt(self._tc, name, fallback)))

    def _put_option(self, name: str, fallback: int, value: int) -> None:  # pragma: no cover
        self._try(lambda: self._cam.put_Option(_opt(self._tc, name, fallback), value))

    def _basic_configure(self) -> None:  # pragma: no cover
        self._try(lambda: self._cam.put_AutoExpoEnable(0))
        self._put_option("TOUPCAM_OPTION_AUTOEXPO_TRIGGER", _OPTION_AUTOEXPO_TRIGGER, 0)
        self._put_option("TOUPCAM_OPTION_RAW", _OPTION_RAW, 1)
        self._put_option(
            "TOUPCAM_OPTION_BITDEPTH", _OPTION_BITDEPTH, 1 if self._bit_depth > 8 else 0
        )
        if not self.is_color_sensor():
            self._put_option("TOUPCAM_OPTION_RGB", _OPTION_RGB, 4 if self._bit_depth > 8 else 3)

    def _prepare_capture_mode(self) -> None:  # pragma: no cover
        self._try(lambda: self._cam.Stop())
        self._drain_state()
        self._put_option("TOUPCAM_OPTION_FLUSH", _OPTION_FLUSH, 3)
        # Start in video mode (required by StartPullModeWithCallback), settle
        # briefly, then switch to software-trigger mode so every capture()
        # call is a deterministic single exposure tied to the gain/exposure
        # just set (see smart_telescope M10 hardware history in the source
        # this was ported from — snap mode staying in free-running video
        # cadence produced stale/black frames).
        self._put_option("TOUPCAM_OPTION_TRIGGER", _OPTION_TRIGGER, 0)
        self._cam.StartPullModeWithCallback(_camera_event, self)
        time.sleep(0.2)
        self._drain_state()
        self._put_option("TOUPCAM_OPTION_TRIGGER", _OPTION_TRIGGER, 1)
        self._put_option("TOUPCAM_OPTION_NOFRAME_TIMEOUT", _OPTION_NOFRAME_TIMEOUT, 1)
        self._put_option("TOUPCAM_OPTION_FLUSH", _OPTION_FLUSH, 3)

    def _capture_raw(self, timeout_s: float) -> np.ndarray:  # pragma: no cover
        self._drain_state()
        self._put_option("TOUPCAM_OPTION_FLUSH", _OPTION_FLUSH, 3)
        self._cam.Trigger(1)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._frame_ready.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
                break
            if self._abort.is_set():
                self._abort.clear()
                raise CaptureAbortedError("TouptekCameraAdapter: capture aborted")
        else:
            raise TimeoutError(f"TouptekCameraAdapter: no frame received within {timeout_s:.1f}s")
        if self._capture_error is not None:
            raise self._capture_error
        return self._pull_pixels()

    def _pull_pixels(self) -> np.ndarray:  # pragma: no cover
        bytes_per_pixel = 2 if self._bit_depth > 8 else 1
        buffer = ctypes.create_string_buffer(self._width * self._height * bytes_per_pixel)
        info = self._tc.ToupcamFrameInfoV2()
        self._cam.PullImageWithRowPitchV2(buffer, self._bit_depth, -1, info)
        width = int(getattr(info, "width", 0) or self._width)
        height = int(getattr(info, "height", 0) or self._height)
        dtype = np.uint16 if bytes_per_pixel == 2 else np.uint8
        pixels = np.frombuffer(buffer, dtype=dtype, count=width * height).reshape((height, width))
        if pixels.dtype != np.uint16:
            pixels = pixels.astype(np.uint16) << 8
        result: np.ndarray = pixels.copy()
        return result

    def _drain_state(self) -> None:
        self._frame_ready.clear()
        self._capture_error = None
        self._abort.clear()

    def _try(self, fn: Any) -> Any:  # noqa: ANN401  # pragma: no cover
        try:
            return fn()
        except Exception as exc:
            _log.warning("TouptekCameraAdapter(%s): SDK call failed: %s", self._logical_name, exc)
            return None


def _camera_event(event: int, ctx: TouptekCameraAdapter) -> None:  # pragma: no cover
    if event in (_EVENT_IMAGE, _EVENT_STILLIMAGE):
        ctx._frame_ready.set()
    elif event == _EVENT_TRIGGER_FAIL:
        ctx._capture_error = RuntimeError("Camera trigger failed")
        ctx._frame_ready.set()
    elif event == _EVENT_DISCONNECTED:
        ctx._capture_error = RuntimeError("Camera disconnected during capture")
        ctx._frame_ready.set()
    elif event == _EVENT_ERROR:
        ctx._capture_error = RuntimeError("Camera reported error during capture")
        ctx._frame_ready.set()


def _normalise_camera_name(value: str) -> str:
    return value.upper().replace(" ", "").replace("_", "")


def _opt(module: Any, name: str, fallback: int) -> int:  # noqa: ANN401
    return int(getattr(module, name, fallback))
