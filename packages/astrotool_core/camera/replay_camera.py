"""ReplayCamera — CameraPort that serves prerecorded frames.

Ported from smart_telescope's ``adapters.replay.camera`` module, merging
its two classes (disk-backed ``ReplayCamera``, in-memory ``ReplayCameraAdapter``)
into one class with two constructors — this project keeps one adapter file
per subsystem role rather than splitting by frame source.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
from astropy.io import fits

from astrotool_core.camera.capabilities import (
    CameraCapabilities,
    CameraDescriptor,
    ConversionGain,
)
from astrotool_core.camera.port import CameraPort, CaptureAbortedError
from astrotool_core.frames.frame import Frame
from astrotool_core.testing.replay_dataset import discover_fits_paths

_REPLAY_CAPABILITIES = CameraCapabilities(
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


class ReplayCamera(CameraPort):
    """Cycles through a fixed sequence of prerecorded frames.

    Useful for integration/golden-master tests that need real (or
    synthetic-but-realistic) image data without live hardware.
    """

    def __init__(self, frames: list[Frame], *, cycle: bool = True) -> None:
        if not frames:
            raise ValueError("ReplayCamera requires at least one frame")
        self._frames = frames
        self._cycle = cycle
        self._index = 0
        self._exposure_ms = 2000.0
        self._gain = 100
        self._black_level = 0
        self._conversion_gain = ConversionGain.LCG

    @classmethod
    def from_directory(cls, dir_path: Path | str, *, cycle: bool = True) -> ReplayCamera:
        """Discover all FITS files in *dir_path* (sorted) and build a ReplayCamera."""
        paths = discover_fits_paths(dir_path)
        if not paths:
            raise ValueError(f"ReplayCamera.from_directory: no FITS files under {dir_path}")
        frames = [Frame.from_fits_bytes(path.read_bytes()) for path in paths]
        return cls(frames, cycle=cycle)

    @classmethod
    def from_arrays(
        cls,
        arrays: list[np.ndarray],
        *,
        bit_depth: int = 16,
        cycle: bool = True,
    ) -> ReplayCamera:
        """Build a ReplayCamera directly from in-memory pixel arrays (no disk I/O)."""
        frames = [
            Frame(
                pixels=array.astype(np.float32),
                header=fits.Header(),
                exposure_seconds=1.0,
                bit_depth=bit_depth,
            )
            for array in arrays
        ]
        return cls(frames, cycle=cycle)

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capture(self, exposure_seconds: float) -> Frame:
        """Return the next frame in the sequence.

        Raises:
            CaptureAbortedError: when exhausted and ``cycle=False``.
        """
        if self._index >= len(self._frames):
            if self._cycle:
                self._index = 0
            else:
                raise CaptureAbortedError("ReplayCamera: frame sequence exhausted")
        frame = replace(self._frames[self._index], exposure_seconds=exposure_seconds)
        self._index += 1
        return frame

    def get_exposure_ms(self) -> float:
        return self._exposure_ms

    def set_exposure_ms(self, ms: float) -> None:
        self._exposure_ms = max(0.1, ms)

    def get_gain(self) -> int:
        return self._gain

    def set_gain(self, gain: int) -> None:
        self._gain = max(_REPLAY_CAPABILITIES.min_gain, gain)

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
            logical_name="ReplayCamera",
            capabilities=_REPLAY_CAPABILITIES,
        )

    @property
    def frame_index(self) -> int:
        """Number of frames served so far."""
        return self._index

    def reset(self) -> None:
        """Rewind the sequence to the first frame."""
        self._index = 0
