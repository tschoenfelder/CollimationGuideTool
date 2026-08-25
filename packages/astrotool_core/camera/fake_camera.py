"""FakeCamera — a CameraPort with no real hardware, for default dev config.

Deliberately minimal: unlike ``testing.fake_touptek.FakeTouptekCamera``
(which reproduces the mock's configurable failure modes for contract/unit
tests), this is the adapter both apps wire to by default so they run and
show a live image with zero hardware present (Stage 7's "demoable on
Windows" requirement).
"""

from __future__ import annotations

import numpy as np

from astrotool_core.camera.capabilities import (
    CameraCapabilities,
    CameraDescriptor,
    ConversionGain,
)
from astrotool_core.camera.port import CameraPort
from astrotool_core.frames.frame import Frame

_FAKE_CAPABILITIES = CameraCapabilities(
    min_gain=100,
    max_gain=3200,
    min_exposure_ms=0.1,
    max_exposure_ms=60_000.0,
    supports_cooling=False,
    supports_hcg=False,
    supports_lcg=False,
    supports_hdr=False,
    supports_black_level=False,
    bit_depth=16,
    pixel_size_um=2.4,
    sensor_width_px=640,
    sensor_height_px=480,
)


def _star_field(width: int, height: int) -> np.ndarray:
    yy, xx = np.indices((height, width), dtype=np.float32)
    image = np.full((height, width), 100.0, dtype=np.float32)
    image += 3000.0 * np.exp(
        -(((xx - width / 2) ** 2 + (yy - height / 2) ** 2) / (2.0 * 2.5**2))
    )
    return image


class FakeCamera(CameraPort):
    def __init__(self, *, fail_connect: bool = False) -> None:
        self._fail_connect = fail_connect
        self._connected = False
        self._exposure_ms = 1000.0
        self._gain = 100
        self._black_level = 0
        self._conversion_gain = ConversionGain.LCG

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("FakeCamera: connect failed (simulated)")
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def capture(self, exposure_seconds: float) -> Frame:
        caps = _FAKE_CAPABILITIES
        pixels = _star_field(caps.sensor_width_px, caps.sensor_height_px)
        return Frame(
            pixels=pixels,
            header={},
            exposure_seconds=exposure_seconds,
            bit_depth=caps.bit_depth,
        )

    def get_exposure_ms(self) -> float:
        return self._exposure_ms

    def set_exposure_ms(self, ms: float) -> None:
        self._exposure_ms = max(0.1, ms)

    def get_gain(self) -> int:
        return self._gain

    def set_gain(self, gain: int) -> None:
        self._gain = max(_FAKE_CAPABILITIES.min_gain, gain)

    def get_black_level(self) -> int:
        return self._black_level

    def set_black_level(self, level: int) -> None:
        self._black_level = max(0, level)

    def get_conversion_gain(self) -> ConversionGain:
        return self._conversion_gain

    def set_conversion_gain(self, mode: ConversionGain) -> None:
        self._conversion_gain = mode

    def get_temperature(self) -> float | None:
        return None

    def get_descriptor(self) -> CameraDescriptor:
        return CameraDescriptor(
            serial_number="FAKE-0001",
            logical_name="FakeCamera",
            capabilities=_FAKE_CAPABILITIES,
        )
