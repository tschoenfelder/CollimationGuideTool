"""FakeTouptekCamera — CameraPort test double reproducing MockCamera's
configurable failure modes.

Ported from smart_telescope's ``adapters.mock.camera.MockCamera``. Used
in contract tests alongside the real ``touptek_adapter`` (Stage 3) so both
are proven to satisfy the same CameraPort contract.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

from astrotool_core.camera.capabilities import (
    CameraCapabilities,
    CameraDescriptor,
    ConversionGain,
)
from astrotool_core.camera.port import CameraPort, CaptureAbortedError
from astrotool_core.frames.frame import Frame

_FAKE_TOUPTEK_CAPABILITIES = CameraCapabilities(
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
    sensor_width_px=3096,
    sensor_height_px=2080,
)


def _bright_pixels() -> np.ndarray[Any, np.dtype[Any]]:
    """64x64 noisy star field — high positive SNR for quality-filter tests."""
    rng = np.random.default_rng(42)
    pixels = rng.normal(100.0, 10.0, (64, 64)).astype(np.float32)
    n = 64 * 64 // 50
    pixels[rng.integers(0, 64, n), rng.integers(0, 64, n)] += 1000.0
    return pixels


def _dim_pixels() -> np.ndarray[Any, np.dtype[Any]]:
    """64x64 cloud-covered frame — SNR well below 30% of _bright_pixels SNR."""
    rng = np.random.default_rng(99)
    pixels = rng.normal(100.0, 10.0, (64, 64)).astype(np.float32)
    n = 64 * 64 // 50
    pixels[rng.integers(0, 64, n), rng.integers(0, 64, n)] += 10.0
    return pixels


class FakeTouptekCamera(CameraPort):
    def __init__(
        self,
        *,
        fail_connect: bool = False,
        fail_on_capture: int | None = None,
        return_bright: bool = False,
        dim_on_captures: frozenset[int] | None = None,
        capture_delay_s: float = 0.0,
    ) -> None:
        self._fail_connect = fail_connect
        self._fail_on_capture = fail_on_capture
        self._return_bright = return_bright
        self._dim_on_captures: frozenset[int] = dim_on_captures or frozenset()
        self._capture_count = 0
        self._exposure_ms = 2000.0
        self._gain = 100
        self._black_level = 0
        self._conversion_gain = ConversionGain.LCG
        self._capture_delay_s = capture_delay_s
        self._abort = threading.Event()

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("FakeTouptekCamera: connect failed (simulated)")

    def disconnect(self) -> None:
        pass

    def capture(self, exposure_seconds: float) -> Frame:
        self._capture_count += 1
        if self._fail_on_capture is not None and self._capture_count == self._fail_on_capture:
            raise RuntimeError(f"FakeTouptekCamera: capture failed (call #{self._capture_count})")
        if self._capture_delay_s > 0.0 and self._abort.wait(timeout=self._capture_delay_s):
            self._abort.clear()
            raise CaptureAbortedError("FakeTouptekCamera: capture aborted")
        if self._capture_count in self._dim_on_captures:
            pixels: np.ndarray[Any, np.dtype[Any]] = _dim_pixels()
        elif self._return_bright:
            pixels = _bright_pixels()
        else:
            pixels = np.zeros(
                (
                    _FAKE_TOUPTEK_CAPABILITIES.sensor_height_px,
                    _FAKE_TOUPTEK_CAPABILITIES.sensor_width_px,
                ),
                dtype=np.float32,
            )
        return Frame(
            pixels=pixels,
            header={},
            exposure_seconds=exposure_seconds,
            bit_depth=_FAKE_TOUPTEK_CAPABILITIES.bit_depth,
        )

    def abort_capture(self) -> None:
        self._abort.set()

    def get_exposure_ms(self) -> float:
        return self._exposure_ms

    def set_exposure_ms(self, ms: float) -> None:
        self._exposure_ms = max(0.1, ms)

    def get_gain(self) -> int:
        return self._gain

    def set_gain(self, gain: int) -> None:
        self._gain = max(_FAKE_TOUPTEK_CAPABILITIES.min_gain, gain)

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
            serial_number="",
            logical_name="FakeTouptekCamera",
            capabilities=_FAKE_TOUPTEK_CAPABILITIES,
        )
