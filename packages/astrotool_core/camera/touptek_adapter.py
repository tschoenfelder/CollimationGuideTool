"""Native ToupTek camera adapter.

Ported from smart_telescope's ``adapters.touptek.managed.SmartTouptekCamera``,
trimmed to what a single-camera collimation/guide tool needs: connect,
capture, exposure/gain/black-level/conversion-gain, temperature/cooling, and
descriptor. Dropped entirely (out of scope for these tools, and each one
carries real smart_telescope-specific complexity that would need its own
characterization pass): filter-wheel control, the multi-"role"
camera-selector/conflict-validation machinery, setup profiles, and capture
priming.

``_detect_pixel_shift`` and the ``EnumV2()``-at-most-once-per-process guard
are ported byte-for-byte — see CONTRIBUTING.md's characterization-test rule
and ``tests/core/camera/test_touptek_adapter_characterization.py``.
"""

from __future__ import annotations

import contextlib
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
from astrotool_core.config import camera_settings
from astrotool_core.frames.frame import Frame
from astrotool_core.frames.pixel_format import BayerPattern
from astrotool_core.timing import SYSTEM_CLOCK, Clock

_log = logging.getLogger(__name__)

# SDK constants, keyed by their SDK name. The single source is the SDK module
# itself: flags and options are looked up there by name (`_sdk_constant`); the
# values here are only the fallbacks for a module that lacks a name, and the
# event/HRESULT values the adapter compares against. A call site passes only
# the name, so it cannot pair a name with the wrong fallback. Every value must
# equal the vendored resources/touptek/toupcam.py -- enforced by
# tests/core/camera/test_touptek_adapter_simulated.py (S6.5a: a copied
# `_FLAG_MONO = 0x40` was really TOUPCAM_FLAG_USB30, so "is it colour?"
# answered "is it not USB3?").
_SDK_FALLBACKS: dict[str, int] = {
    "TOUPCAM_EVENT_IMAGE": 0x0004,
    "TOUPCAM_EVENT_STILLIMAGE": 0x0005,
    "TOUPCAM_EVENT_TRIGGERFAIL": 0x0007,
    "TOUPCAM_EVENT_ERROR": 0x0080,
    "TOUPCAM_EVENT_DISCONNECTED": 0x0081,
    "TOUPCAM_FLAG_MONO": 0x00000010,
    "TOUPCAM_FLAG_USB30": 0x00000040,  # USB3-capable camera, not the actual link
    "TOUPCAM_FLAG_USB30_OVER_USB20": 0x00000100,  # USB3 camera on a USB2 link
    "TOUPCAM_FLAG_TEC": 0x00000080,
    "TOUPCAM_FLAG_RAW16": 0x00008000,  # camera has true 16-bit ADC depth
    "TOUPCAM_FLAG_TEC_ONOFF": 0x00020000,
    "TOUPCAM_FLAG_BLACKLEVEL": 0x00400000,
    "TOUPCAM_FLAG_CG": 0x04000000,
    "TOUPCAM_FLAG_CGHDR": 0x0000000800000000,
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
    "E_NOTIMPL": 0x80004001,  # this camera model has no such feature
    "E_BUSY": 0x800700AA,  # the device already has an open handle
}


# S6.5 (#55 audit C01): the capability table -- ONE decision per feature for the
# connected model. A flag-gated feature is supported when the model's SDK flag
# word has any of its flags (names looked up like every other SDK constant);
# `None` = not flag-gated, i.e. supported on a connected camera unless the SDK
# answers E_NOTIMPL (the temperature sensor: some models report a temperature
# without a TEC -- audit C03 -- and the GPCMOS02000KPA has none at all, 4730c55;
# it is probed once at connect). Whatever the flags say, the first E_NOTIMPL for
# a feature (or for an option outside the table) is recorded for the rest of the
# connection (`_refused`) and reported once; nothing asks again (531a952/4730c55:
# per-frame/per-poll retries of a refused call spammed a WARNING for a whole
# session). The value ranges (gain, exposure, TEC target) are asked once per
# connect, never per frame (`get_descriptor()` is read on every frame); a gain or
# exposure range whose query failed transiently is asked again at most once per
# `range_retry_s`. Safety (review C1): the cooler's on/off ("cooling") is its own
# feature, separate from the target ("cooling_target") -- a refused target never
# suppresses a TEC-off (see `_tec_off`).
_FEATURE_FLAGS: dict[str, tuple[str, ...] | None] = {
    "cooling": ("TOUPCAM_FLAG_TEC", "TOUPCAM_FLAG_TEC_ONOFF"),
    "cooling_target": ("TOUPCAM_FLAG_TEC", "TOUPCAM_FLAG_TEC_ONOFF"),
    "conversion_gain": ("TOUPCAM_FLAG_CG", "TOUPCAM_FLAG_CGHDR"),  # HCG (LCG always)
    "hdr": ("TOUPCAM_FLAG_CGHDR",),
    "black_level": ("TOUPCAM_FLAG_BLACKLEVEL",),
    "raw16": ("TOUPCAM_FLAG_RAW16",),  # true 16-bit ADC: no pixel shift
    "temperature": None,
    "target_temp_range": ("TOUPCAM_FLAG_TEC", "TOUPCAM_FLAG_TEC_ONOFF"),
    "gain_range": None,
    "exposure_range": None,
}

#: The SDK options that belong to a feature: refusing one refuses the feature.
_OPTION_FEATURE: dict[str, str] = {
    "TOUPCAM_OPTION_TEC": "cooling",
    "TOUPCAM_OPTION_TECTARGET": "cooling_target",
    "TOUPCAM_OPTION_CG": "conversion_gain",
    "TOUPCAM_OPTION_BLACKLEVEL": "black_level",
}


def _is_not_implemented(exc: Exception) -> bool:
    """The SDK's "this camera model has no such feature" answer (a model
    property, never transient). The one place E_NOTIMPL is recognised."""
    hr = getattr(exc, "hr", None)
    return isinstance(hr, int) and (hr & 0xFFFFFFFF) == _SDK_FALLBACKS["E_NOTIMPL"]


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


def _validated_sdk_bit_depth(sdk_bit_depth: int) -> int:
    """Sanity-check get_RawFormat()'s directly-reported ADC bit depth.

    Deliberately does NOT derive a buffer pixel_shift from this value —
    an earlier version of this fix did (`16 - sdk_bit_depth`, mirroring
    `_detect_pixel_shift`'s convention) on the assumption that the SDK
    always delivers 16-bit output as sub-range data left-shifted
    ("MSB-aligned") to fill it. That assumption doesn't hold for every
    camera: confirmed on real hardware, the GPCMOS02000KPA's raw buffer
    already delivers unpadded ~12-bit codes directly (0-4095, no
    left-shift applied), which `_detect_pixel_shift` already correctly
    recognizes as shift=0 whenever it can run — deriving shift=4 from
    the SDK's bit-depth report instead divided every real pixel value by
    16 on top of that, corrupting captures (observed: max pixel value
    dropped from ~4094 to 255). So this value is used *only* for what
    ceiling to report/measure against (bit_depth), independent of
    whatever `_detect_pixel_shift` finds for the buffer's own layout.
    Confirmed on real hardware: the GPCMOS02000KPA reports 12-bit
    directly, matching its true ~4095 saturation ceiling seen in
    captured frames; the ATR585M and G3M678M both correctly report
    16-bit. Returns -1 (unknown) for a nonsensical value so the caller
    falls back to the pixel_shift-derived bit_depth instead.
    """
    if not 1 <= sdk_bit_depth <= 16:
        return -1
    return sdk_bit_depth


_sdk_lifecycle_lock = threading.RLock()

# EnumV2() must be called at most once per process (ported guard — see
# smart_telescope M10-058: a real Pi crash traced to toupcam.py's
# EnumV2()->__initlib() setting ctypes _fields_ unconditionally on every
# call; under Python 3.13 a second call raises "AttributeError: _fields_ is
# final", and the next native SDK call after that segfaulted the process).
_enum_devices_cache: list[Any] | None = None

# Issue #40: a real-reported "selected G3M678M, connected GPCMOS02000"
# device-identity mix-up. `camera_id` (the SDK's own opaque `.id`) is
# already used for selection (see `_select_device`), but nothing
# previously cross-checked that the ACTUALLY-opened handle's own
# independently-queried `SerialNumber()` was consistent between
# connects to the same id. Process-lifetime memory (like
# `_enum_devices_cache` above), not per-adapter-instance, since a fresh
# `TouptekCameraAdapter` is constructed on every Connect click.
_known_serials: dict[str, str] = {}


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


def _describe_sdk_error(name: str, exc: Exception) -> str:
    """Human-readable text for an SDK failure while opening a camera (issue
    #45). The SDK raises HRESULTException(hr); 0x800700AA means the device is
    already open (e.g. by another panel or a handle that was never released)."""
    raw = getattr(exc, "hr", None)
    if raw is None:
        try:
            raw = int(str(exc))
        except ValueError:
            raw = None
    if isinstance(raw, int) and (raw & 0xFFFFFFFF) == _SDK_FALLBACKS["E_BUSY"]:
        return (
            f"TouptekCameraAdapter({name}): device busy (0x800700AA) -- it is already "
            "open elsewhere (another panel or an unreleased handle)"
        )
    code = f" (0x{raw & 0xFFFFFFFF:08X})" if isinstance(raw, int) else ""
    return f"TouptekCameraAdapter({name}): SDK error while opening{code}: {exc}"


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
        clock: Clock | None = None,
        range_retry_s: float = 30.0,
    ) -> None:
        #: Paces the bounded re-ask of a failed range query (review C4).
        self._clock: Clock = clock if clock is not None else SYSTEM_CLOCK
        self._range_retry_s = range_retry_s
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
        # The sensor's true ADC bit depth as reported directly by the SDK
        # (get_RawFormat()) — see _query_bit_depth_from_sdk's docstring.
        # -1=unknown/query failed, fall back to 16 - pixel_shift.
        # Deliberately independent of _pixel_shift: that governs whether
        # the *buffer* needs right-shifting to reach native ADC codes (a
        # data-layout question _detect_pixel_shift already answers
        # correctly per frame), while this is purely about what ceiling to
        # report/measure against — the two aren't the same sensor property
        # and conflating them once corrupted real data (a since-reverted
        # attempt at this fix forced pixel_shift from this value directly:
        # on the GPCMOS02000KPA, whose buffer already delivers unpadded
        # 12-bit codes with no shift needed, that divided every real pixel
        # value by 16 on top of the correct shift of 0).
        self._sdk_bit_depth: int = -1
        # See _capture_connected's docstring (issue #14) — tracks whether
        # set_exposure_ms() has ever been called, so capture()'s parameter
        # only bootstraps the very first exposure rather than permanently
        # overriding whatever was set afterward on every subsequent frame.
        self._exposure_ever_set = False
        # Cooling: _basic_configure() forces the real TEC off on every
        # connect (never trusts a pre-existing ON state -- e.g. left on by a
        # crashed prior session), so this always starts False; _target_temperature_c
        # is a user intention, not read back from hardware -- see set_target_temperature.
        self._cooling_enabled = False
        # D04: the built-in default has one owner (config.camera_settings).
        self._target_temperature_c: float | None = camera_settings.DEFAULT_TARGET_TEMPERATURE_C
        # S6.5: features/options the SDK answered E_NOTIMPL for on this connection
        # (see `_FEATURE_FLAGS`), and the ranges asked once at connect.
        # Threading: read and extended from the GUI thread (polls, descriptor) and the
        # capture worker (options); single `in`/`add`/`update` set operations are
        # atomic under the GIL, and the set is only ever *replaced* (never mutated
        # in place) on connect/disconnect, so a reader sees either the old or the
        # new connection's set. A lost race at worst repeats one SDK call.
        self._refused: set[str] = set()
        self._gain_range: tuple[int, int] | None = None
        self._exposure_range_ms: tuple[float, float] | None = None
        self._target_range_c: tuple[float, float] | None = None
        #: Clock time of the last range probe (None before connect).
        self._ranges_probed_at: float | None = None
        #: Review C1: the "cooling off" re-try after a refused TEC on/off was made.
        self._tec_off_retried = False

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
        self._refused = set()
        self._tec_off_retried = False
        self._log_model_flag(str(getattr(device.model, "name", "") or self._logical_name))
        if self._supports("raw16"):
            self._pixel_shift = 0  # true 16-bit sensor — no shift needed
        try:
            self._serial_number = cam.SerialNumber()
        except Exception:
            self._serial_number = ""
        if self._serial_number:
            remembered = _known_serials.get(self._device_id)
            if remembered is not None and remembered != self._serial_number:
                with contextlib.suppress(Exception):
                    cam.Close()
                self._cam = None
                raise ConnectionError(
                    f"TouptekCameraAdapter: device id {self._device_id!r} previously "
                    f"reported serial {remembered!r}, now reports "
                    f"{self._serial_number!r} -- refusing this connection (the "
                    f"physical device behind this id may have changed)."
                )
            _known_serials[self._device_id] = self._serial_number
        try:
            self._width, self._height = cam.get_Size()
        except Exception:
            self._width = int(device.model.res[0].width)
            self._height = int(device.model.res[0].height)

        try:
            self._basic_configure()
            self._prepare_capture_mode()
            self._sdk_bit_depth = self._query_bit_depth_from_sdk()
            self._probe_capabilities()
        except Exception as exc:
            # Issue #45: a half-opened device must never keep its SDK handle --
            # the SDK allows one handle per device, so a leaked one makes the
            # next connect to this camera fail ERROR_BUSY (0x800700AA).
            self._abandon_handle()
            raise ConnectionError(_describe_sdk_error(self._logical_name, exc)) from exc

    def _log_model_flag(self, model_name: str) -> None:
        """S6.5b: the raw SDK flag word and what this adapter derives from it,
        once per connect -- the rig cameras' real MONO/USB30/TEC bits were
        only inferred (S6.5a); this line makes them known from a Pi log."""
        _log.info(
            "TouptekCameraAdapter(%s): opened model=%s model.flag=0x%X -> %s, "
            "USB3-capable=%s, USB3-over-USB2-link=%s, TEC=%s",
            self._logical_name,
            model_name,
            self._model_flag,
            "colour" if self.is_color_sensor() else "mono",
            "yes" if self._has_flag("TOUPCAM_FLAG_USB30") else "no",
            "yes" if self._has_flag("TOUPCAM_FLAG_USB30_OVER_USB20") else "no",
            "yes" if self._supports("cooling") else "no",
        )

    def _probe_capabilities(self) -> None:  # pragma: no cover -- simulator-tested
        """S6.5: the table's non-flag decisions, once per connect -- the
        temperature sensor (one probe; E_NOTIMPL refuses it) and the value
        ranges `get_descriptor()` reports (asked here, never per frame; a failed
        query warns once and leaves the documented default)."""
        self._sdk_call("temperature", lambda: self._cam.get_Temperature())
        self._probe_ranges()
        self._target_range_c = self._query_target_temp_range()

    def _probe_ranges(self) -> None:  # pragma: no cover -- simulator-tested
        """Ask each gain/exposure range still unknown (and not refused) once."""
        self._ranges_probed_at = self._clock.monotonic()
        if self._gain_range is None and self._supports("gain_range"):
            gain = self._sdk_call("gain_range", lambda: self._cam.get_ExpoAGainRange())
            self._gain_range = (int(gain[0]), int(gain[1])) if gain else None
        if self._exposure_range_ms is None and self._supports("exposure_range"):
            exposure = self._sdk_call("exposure_range", lambda: self._cam.get_ExpTimeRange())
            self._exposure_range_ms = (
                (float(exposure[0]) / 1000.0, float(exposure[1]) / 1000.0) if exposure else None
            )

    def _reprobe_failed_ranges(self) -> None:
        """Review C4: a range whose query failed transiently (not E_NOTIMPL) is asked
        again at most once per `range_retry_s` on the clock -- never per frame, and
        never once known or refused."""
        if self._cam is None or self._ranges_probed_at is None:
            return
        missing = (self._gain_range is None and self._supports("gain_range")) or (
            self._exposure_range_ms is None and self._supports("exposure_range")
        )
        if missing and self._clock.monotonic() - self._ranges_probed_at >= self._range_retry_s:
            self._probe_ranges()

    def _supports(self, feature: str) -> bool:
        """The capability table's decision for *feature* (see `_FEATURE_FLAGS`)."""
        if feature in self._refused:
            return False
        flags = _FEATURE_FLAGS[feature]
        if flags is None:
            return self._cam is not None
        return any(self._model_flag & _sdk_constant(self._tc, flag) for flag in flags)

    def _refuse(self, subject: str) -> None:
        """Record an E_NOTIMPL answer for *subject* (a feature or an SDK option
        name) for the rest of this connection, and say so once."""
        feature = _OPTION_FEATURE.get(subject, subject)
        if feature in self._refused:
            return
        self._refused.update({subject, feature})
        _log.info(
            "TouptekCameraAdapter(%s): %s not implemented by this camera (SDK E_NOTIMPL) "
            "-- treated as unsupported until the next connect",
            self._logical_name,
            feature,
        )

    def _sdk_call(self, subject: str, fn: Any) -> Any:  # noqa: ANN401
        """`fn()`; on E_NOTIMPL the table records *subject* as unsupported, any
        other failure is warned about (and may be transient). None on failure."""
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- SDK raises HRESULTException
            if _is_not_implemented(exc):
                self._refuse(subject)
            else:
                _log.warning(
                    "TouptekCameraAdapter(%s): SDK call failed: %s", self._logical_name, exc
                )
            return None

    @property
    def device_id(self) -> str:
        """SDK id of the device actually opened (empty before connect)."""
        return self._device_id

    def _abandon_handle(self) -> None:
        cam, self._cam = self._cam, None
        self._tc = None
        if cam is not None:
            with contextlib.suppress(Exception):
                cam.Close()

    def disconnect(self) -> None:
        if self._cam is not None:  # pragma: no cover
            with _sdk_lifecycle_lock:
                try:
                    # Never leave the cooler running unattended once this adapter stops
                    # tracking the camera -- same "deactivate before disconnect" safety
                    # pattern as the mount's stop_tracking()-before-disconnect. Skipped
                    # only on hardware whose flags say it has no TEC at all.
                    self._tec_off(final=True)
                    self._cam.Stop()
                finally:
                    self._cam.Close()
        self._cam = None
        self._tc = None
        self._cooling_enabled = False
        # S6.5: the table is decided again on the next connect.
        self._refused = set()
        self._gain_range = self._exposure_range_ms = self._target_range_c = None
        self._ranges_probed_at = None
        self._tec_off_retried = False
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
                bit_depth=self._effective_bit_depth(shift),
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

    # Real-field report (2026-09-29): every capability-gated SDK option used to
    # be called unconditionally whenever _cam is set -- on G3M678M (no TEC),
    # get_descriptor() (read on every delivered frame) spammed a "SDK call
    # failed" warning for a whole session (531a952). Every call site below asks
    # the capability table (`_supports`, S6.5) and skips the SDK entirely for an
    # unsupported feature.
    def get_black_level(self) -> int:
        if self._cam is not None and self._supports("black_level"):  # pragma: no cover
            value = self._get_option("TOUPCAM_OPTION_BLACKLEVEL")
            if value is not None:
                return int(value)
        return 0

    def set_black_level(self, level: int) -> None:
        if self._cam is not None and self._supports("black_level"):  # pragma: no cover
            self._put_option("TOUPCAM_OPTION_BLACKLEVEL", max(0, int(level)))
            self._pixel_shift = -1  # offset changes 16-bit alignment; re-detect on next frame

    def get_conversion_gain(self) -> ConversionGain:
        if self._cam is not None and self._supports("conversion_gain"):  # pragma: no cover
            value = self._get_option("TOUPCAM_OPTION_CG")
            if value is not None:
                try:
                    return ConversionGain(int(value))
                except ValueError:
                    pass
        return ConversionGain.LCG

    def set_conversion_gain(self, mode: ConversionGain) -> None:
        if self._cam is not None and self._supports("conversion_gain"):  # pragma: no cover
            self._put_option("TOUPCAM_OPTION_CG", int(mode))

    def get_temperature(self) -> float | None:
        # Real-field report (4730c55): GPCMOS02000KPA has no temperature sensor
        # -- get_Temperature() raised E_NOTIMPL on every poll tick (the panel
        # polls on a timer), a WARNING every ~0.3-1 s. The table decides it once
        # per connect (`_probe_capabilities`); a non-E_NOTIMPL failure is
        # transient: warned, asked again on the next poll.
        if self._cam is None or not self._supports("temperature"):
            return None
        value = self._sdk_call("temperature", lambda: self._cam.get_Temperature())
        return None if value is None else round(float(value) / 10.0, 1)

    def get_cooling_enabled(self) -> bool:
        if self._cam is not None and self._supports("cooling"):  # pragma: no cover
            value = self._get_option("TOUPCAM_OPTION_TEC")
            if value is not None:
                return bool(value)
        return self._cooling_enabled

    def set_cooling_enabled(self, enabled: bool) -> None:
        self._cooling_enabled = bool(enabled)
        if self._cam is None:
            return
        if not enabled:
            self._tec_off(final=False)  # safety: see _tec_off
        elif self._supports("cooling"):  # pragma: no cover
            self._put_option("TOUPCAM_OPTION_TEC", 1)

    def _has_cooler(self) -> bool:
        """The model's flags say it has a TEC -- the flags alone, never a refusal."""
        return any(
            self._model_flag & _sdk_constant(self._tc, flag)
            for flag in _FEATURE_FLAGS["cooling"] or ()
        )

    def _tec_off(self, *, final: bool) -> None:
        """Safety write TEC = 0 (review C1). Gated by the TEC flags only: a refusal of
        any OTHER option (e.g. the target) never suppresses it. Only an earlier E_NOTIMPL
        for the on/off option itself limits it -- then it is still attempted once more
        for "cooling off" per connection, and always at disconnect (`final`), and
        logged."""
        if self._cam is None or not self._has_cooler():
            return
        name = "TOUPCAM_OPTION_TEC"
        if name in self._refused:
            if not final and self._tec_off_retried:
                return
            self._tec_off_retried = self._tec_off_retried or not final
            _log.info(
                "TouptekCameraAdapter(%s): TEC off attempted again despite an earlier "
                "E_NOTIMPL for the cooler on/off (safety, %s)",
                self._logical_name,
                "disconnect" if final else "cooling off",
            )
        self._sdk_call(name, lambda: self._cam.put_Option(_sdk_constant(self._tc, name), 0))

    def _target_supported(self) -> bool:
        return self._supports("cooling") and self._supports("cooling_target")

    def get_target_temperature(self) -> float | None:
        if self._cam is not None and self._target_supported():  # pragma: no cover
            value = self._get_option("TOUPCAM_OPTION_TECTARGET")
            if value is not None:
                return round(int(value) / 10.0, 1)
        return self._target_temperature_c

    def set_target_temperature(self, celsius: float) -> None:
        self._target_temperature_c = float(celsius)
        if self._cam is not None and self._target_supported():  # pragma: no cover
            self._put_option("TOUPCAM_OPTION_TECTARGET", int(round(celsius * 10)))

    def _query_target_temp_range(self) -> tuple[float, float] | None:  # pragma: no cover
        """Asked once per connect (`_probe_capabilities`)."""
        if self._cam is None or not (
            self._supports("cooling") and self._supports("target_temp_range")
        ):
            return None
        rng = self._sdk_call("target_temp_range", lambda: self._cam.get_TecTargetRange())
        if not rng:
            return None
        return round(rng[0] / 10.0, 1), round(rng[1] / 10.0, 1)

    def get_descriptor(self) -> CameraDescriptor:
        """Built from the capability table; makes no SDK call (S6.5: read on
        every frame, so the ranges are the ones asked once at connect, plus the
        bounded re-ask of a transiently failed one, `_reprobe_failed_ranges`)."""
        self._reprobe_failed_ranges()
        min_gain, max_gain = self._gain_range if self._gain_range is not None else (100, 100)
        min_exp_ms, max_exp_ms = (
            self._exposure_range_ms if self._exposure_range_ms is not None else (2000.0, 2000.0)
        )
        target_range = self._target_range_c if self._supports("cooling") else None
        min_target_temp_c = target_range[0] if target_range is not None else None
        max_target_temp_c = target_range[1] if target_range is not None else None
        capabilities = CameraCapabilities(
            min_gain=min_gain,
            max_gain=max_gain,
            min_exposure_ms=min_exp_ms,
            max_exposure_ms=max_exp_ms,
            supports_cooling=self._supports("cooling"),
            supports_hcg=self._supports("conversion_gain"),
            supports_lcg=True,  # LCG is the SDK's default conversion gain on every model
            supports_hdr=self._supports("hdr"),
            supports_black_level=self._supports("black_level"),
            bit_depth=(
                self._effective_bit_depth(max(0, self._pixel_shift)) if self._bit_depth > 8 else 8
            ),
            pixel_size_um=0.0,
            sensor_width_px=self._width,
            sensor_height_px=self._height,
            min_target_temp_c=min_target_temp_c,
            max_target_temp_c=max_target_temp_c,
            supports_temperature=self._supports("temperature"),
            supports_raw16=self._supports("raw16"),
        )
        return CameraDescriptor(
            serial_number=self._serial_number,
            logical_name=self._logical_name,
            capabilities=capabilities,
        )

    def is_color_sensor(self) -> bool:
        return not self._has_flag("TOUPCAM_FLAG_MONO")

    def _has_flag(self, name: str) -> bool:
        """Whether the connected model's SDK flag word has the SDK flag *name*
        (see `_sdk_constant`)."""
        return bool(self._model_flag & _sdk_constant(self._tc, name))

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

    def _effective_bit_depth(self, shift: int) -> int:
        """Bit depth to report for a captured frame/descriptor: prefer the
        SDK's direct get_RawFormat() report (queried once at connect time,
        see _query_bit_depth_from_sdk) over deriving it from *shift*
        (16 - shift) — the two describe different sensor properties and
        don't always agree (see _validated_sdk_bit_depth's docstring for
        the real-hardware corruption that conflating them once caused).
        Falls back to the shift-derived value when the SDK query wasn't
        available (query failed, or not yet connected).
        """
        if self._sdk_bit_depth > 0:
            return self._sdk_bit_depth
        return 16 - shift

    def _query_bit_depth_from_sdk(self) -> int:
        """Ask the SDK for this camera's native ADC bit depth directly,
        via the same get_RawFormat() call get_bayer_pattern() already
        makes — see `_validated_sdk_bit_depth`'s docstring for why this is
        preferred, for *reporting* purposes, over `16 - pixel_shift`
        (which stays the source of truth for how to actually decode the
        buffer). Returns -1 (unknown) on any failure, so callers fall back
        to `16 - pixel_shift` exactly as before this existed.
        """
        try:  # pragma: no cover — requires real hardware
            _raw_fourcc, sdk_bit_depth = self._cam.get_RawFormat()
        except Exception as exc:
            _log.warning(
                "TouptekCameraAdapter(%s): get_RawFormat() failed while determining bit depth: %s",
                self._logical_name,
                exc,
            )
            return -1
        return _validated_sdk_bit_depth(int(sdk_bit_depth))

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

    def _option_allowed(self, name: str) -> bool:
        """Not refused on this connection, and its feature (if any) supported."""
        feature = _OPTION_FEATURE.get(name)
        return name not in self._refused and (feature is None or self._supports(feature))

    def _get_option(self, name: str) -> Any:  # noqa: ANN401  # pragma: no cover
        if not self._option_allowed(name):
            return None
        return self._sdk_call(name, lambda: self._cam.get_Option(_sdk_constant(self._tc, name)))

    def _put_option(self, name: str, value: int) -> None:  # pragma: no cover
        if self._option_allowed(name):
            self._sdk_call(name, lambda: self._cam.put_Option(_sdk_constant(self._tc, name), value))

    def _basic_configure(self) -> None:  # pragma: no cover
        self._try(lambda: self._cam.put_AutoExpoEnable(0))
        self._put_option("TOUPCAM_OPTION_AUTOEXPO_TRIGGER", 0)
        # Never trust a pre-existing TEC state (e.g. left on by a crashed prior
        # session) -- every connect starts with cooling actively forced off.
        # Skipped entirely on hardware with no TEC at all (the table says no
        # cooling) -- same fix as disconnect()'s TEC-off, above.
        self._tec_off(final=False)
        self._cooling_enabled = False
        self._put_option("TOUPCAM_OPTION_RAW", 1)
        self._put_option("TOUPCAM_OPTION_BITDEPTH", 1 if self._bit_depth > 8 else 0)
        if not self.is_color_sensor():
            self._put_option("TOUPCAM_OPTION_RGB", 4 if self._bit_depth > 8 else 3)

    def _prepare_capture_mode(self) -> None:  # pragma: no cover
        self._try(lambda: self._cam.Stop())
        self._drain_state()
        self._put_option("TOUPCAM_OPTION_FLUSH", 3)
        # Start in video mode (required by StartPullModeWithCallback), settle
        # briefly, then switch to software-trigger mode so every capture()
        # call is a deterministic single exposure tied to the gain/exposure
        # just set (see smart_telescope M10 hardware history in the source
        # this was ported from — snap mode staying in free-running video
        # cadence produced stale/black frames).
        self._put_option("TOUPCAM_OPTION_TRIGGER", 0)
        self._cam.StartPullModeWithCallback(_camera_event, self)
        time.sleep(0.2)
        self._drain_state()
        self._put_option("TOUPCAM_OPTION_TRIGGER", 1)
        # S6.5b: the SDK's own no-frame timeout stays disabled (0). The
        # vendored SDK documents 0 or >= TOUPCAM_NOFRAME_TIMEOUT_MIN (500)
        # milliseconds (toupcam.py:109); the 1 put here before -- the INDI
        # driver's value (indi_toupbase), ported via smart_telescope -- is
        # outside that documented range (what the rig's SDK did with it is
        # unknown).
        # A fixed SDK period cannot follow the exposure (seconds to minutes in
        # trigger mode), _camera_event does not handle its NOFRAMETIMEOUT
        # event, and _capture_raw already bounds every wait by
        # exposure + timeout_extra_s.
        self._put_option("TOUPCAM_OPTION_NOFRAME_TIMEOUT", 0)
        self._put_option("TOUPCAM_OPTION_FLUSH", 3)

    def _capture_raw(self, timeout_s: float) -> np.ndarray:  # pragma: no cover
        self._drain_state()
        self._put_option("TOUPCAM_OPTION_FLUSH", 3)
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
    if event in (_SDK_FALLBACKS["TOUPCAM_EVENT_IMAGE"], _SDK_FALLBACKS["TOUPCAM_EVENT_STILLIMAGE"]):
        ctx._frame_ready.set()
    elif event == _SDK_FALLBACKS["TOUPCAM_EVENT_TRIGGERFAIL"]:
        ctx._capture_error = RuntimeError("Camera trigger failed")
        ctx._frame_ready.set()
    elif event == _SDK_FALLBACKS["TOUPCAM_EVENT_DISCONNECTED"]:
        ctx._capture_error = RuntimeError("Camera disconnected during capture")
        ctx._frame_ready.set()
    elif event == _SDK_FALLBACKS["TOUPCAM_EVENT_ERROR"]:
        ctx._capture_error = RuntimeError("Camera reported error during capture")
        ctx._frame_ready.set()


def _normalise_camera_name(value: str) -> str:
    return value.upper().replace(" ", "").replace("_", "")


def _sdk_constant(module: Any, name: str) -> int:  # noqa: ANN401
    """An SDK constant (flag, option id) by its name on the SDK module --
    the single source; the `_SDK_FALLBACKS` value (equal to the vendored SDK)
    only when the module lacks the name or is not loaded (``None``). An
    unknown *name* raises KeyError: every name used must have a fallback."""
    return int(getattr(module, name, _SDK_FALLBACKS[name]))
