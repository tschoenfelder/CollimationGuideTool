"""Frame — immutable container for a single captured exposure.

Ported from smart_telescope's ``domain.frame.FitsFrame`` and
``domain.collimation.processing.frame.ProcessedFrame``, merged into one
type per PLAN.md Stage 1 (a single frame representation instead of two).
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from astropy.io import fits


@dataclass(frozen=True)
class Frame:
    """A single camera exposure, before any pixel-format/demosaic handling.

    pixels: float32 ndarray shaped (height, width) — mono, or a single
        Bayer-mosaiced plane (see ``pixel_format.py`` for demosaicing).
    header: parsed FITS header, or an empty ``fits.Header()`` when built
        directly from an array rather than from FITS bytes.
    exposure_seconds: value from the EXPTIME header key, or 0.0 if absent.
    bit_depth: sensor bit depth (8 or 16).
    timestamp: capture timestamp (``time.monotonic()`` seconds).
    data: raw FITS bytes (empty when constructed directly from an array).
    """

    pixels: np.ndarray[Any, np.dtype[Any]]
    header: object
    exposure_seconds: float
    bit_depth: int = 16
    timestamp: float = field(default_factory=time.monotonic)
    data: bytes = field(default=b"")

    @property
    def height(self) -> int:
        return int(self.pixels.shape[0])

    @property
    def width(self) -> int:
        return int(self.pixels.shape[1])

    def to_fits_bytes(self) -> bytes:
        """Return raw FITS bytes, serializing from pixels if none are cached."""
        if self.data:
            return self.data
        hdr = self.header if isinstance(self.header, fits.Header) else fits.Header()
        hdu = fits.PrimaryHDU(data=self.pixels, header=hdr)
        buf = io.BytesIO()
        fits.HDUList([hdu]).writeto(buf)
        return buf.getvalue()

    @classmethod
    def from_fits_bytes(cls, raw: bytes, *, bit_depth: int = 16) -> Frame:
        try:
            with fits.open(io.BytesIO(raw)) as hdul:
                hdu = hdul[0]
                header = hdu.header.copy()
                pixels: np.ndarray[Any, np.dtype[Any]] = np.array(hdu.data, dtype=np.float32)
                exposure_seconds = float(header.get("EXPTIME", 0.0))
        except Exception as exc:
            raise ValueError(f"Cannot parse FITS data: {exc}") from exc
        return cls(
            pixels=pixels,
            header=header,
            exposure_seconds=exposure_seconds,
            bit_depth=bit_depth,
            data=raw,
        )
