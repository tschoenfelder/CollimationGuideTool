"""CameraPort — hardware-independent camera control surface.

Ported from smart_telescope's ``ports.camera.CameraPort``, trimmed and
adapted: ``connect()`` returns ``None`` and raises on failure (instead of
returning ``bool``), matching the architecture doc's ``MountPort.connect()``
convention so all three port types (camera/mount/focus) fail the same way.
``get_bit_depth()``/``get_serial_number()``/``get_logical_name()`` are
folded into ``get_descriptor()`` (see ``capabilities.py``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from astrotool_core.camera.capabilities import CameraDescriptor, ConversionGain
from astrotool_core.frames.frame import Frame
from astrotool_core.frames.pixel_format import BayerPattern


class CaptureAbortedError(Exception):
    """Raised by capture() when abort_capture() is called during an exposure."""


class CameraPort(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def capture(self, exposure_seconds: float) -> Frame: ...

    def abort_capture(self) -> None:  # noqa: B027 — deliberate default no-op, not abstract
        """Interrupt an in-progress capture. Default: no-op."""

    @abstractmethod
    def get_exposure_ms(self) -> float: ...

    @abstractmethod
    def set_exposure_ms(self, ms: float) -> None: ...

    @abstractmethod
    def get_gain(self) -> int: ...

    @abstractmethod
    def set_gain(self, gain: int) -> None: ...

    @abstractmethod
    def get_black_level(self) -> int: ...

    @abstractmethod
    def set_black_level(self, level: int) -> None: ...

    @abstractmethod
    def get_conversion_gain(self) -> ConversionGain: ...

    @abstractmethod
    def set_conversion_gain(self, mode: ConversionGain) -> None: ...

    @abstractmethod
    def get_temperature(self) -> float | None: ...

    @abstractmethod
    def get_descriptor(self) -> CameraDescriptor: ...

    def is_color_sensor(self) -> bool:  # noqa: B027 — deliberate default, not abstract
        """True if this camera has a color filter array. Default: mono.

        A caller must not assume every ``CameraPort`` needs demosaicing —
        most test doubles and simple sources are mono — so this defaults
        to False rather than being abstract; ``TouptekCameraAdapter``
        overrides it with the real answer from hardware.
        """
        return False

    def get_bayer_pattern(self) -> BayerPattern:  # noqa: B027 — deliberate default
        """Sensor's Bayer mosaic layout. Meaningful only when
        ``is_color_sensor()`` is True; default: MONO (no filter array)."""
        return BayerPattern.MONO
