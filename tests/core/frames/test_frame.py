import numpy as np
from astropy.io import fits
from astrotool_core.frames.frame import Frame


def test_height_and_width_reflect_pixel_shape() -> None:
    pixels = np.zeros((480, 640), dtype=np.float32)
    frame = Frame(pixels=pixels, header=fits.Header(), exposure_seconds=1.5)
    assert frame.height == 480
    assert frame.width == 640


def test_defaults_bit_depth_and_timestamp() -> None:
    pixels = np.zeros((4, 4), dtype=np.float32)
    frame = Frame(pixels=pixels, header=fits.Header(), exposure_seconds=0.0)
    assert frame.bit_depth == 16
    assert frame.timestamp > 0
    assert frame.data == b""


def test_to_fits_bytes_returns_cached_data_when_present() -> None:
    pixels = np.zeros((4, 4), dtype=np.float32)
    frame = Frame(
        pixels=pixels,
        header=fits.Header(),
        exposure_seconds=1.0,
        data=b"cached-bytes",
    )
    assert frame.to_fits_bytes() == b"cached-bytes"


def test_to_fits_bytes_serializes_from_pixels_when_no_cache() -> None:
    pixels = np.arange(16, dtype=np.float32).reshape(4, 4)
    frame = Frame(pixels=pixels, header=fits.Header(), exposure_seconds=2.0)
    raw = frame.to_fits_bytes()
    assert raw.startswith(b"SIMPLE")


def test_fits_roundtrip_preserves_pixels_and_exposure() -> None:
    pixels = np.arange(16, dtype=np.float32).reshape(4, 4)
    header = fits.Header()
    header["EXPTIME"] = 3.25
    original = Frame(pixels=pixels, header=header, exposure_seconds=3.25)

    raw = original.to_fits_bytes()
    restored = Frame.from_fits_bytes(raw)

    assert np.allclose(restored.pixels, pixels)
    assert restored.exposure_seconds == 3.25
    assert restored.data == raw


def test_from_fits_bytes_defaults_exposure_when_header_lacks_exptime() -> None:
    pixels = np.zeros((4, 4), dtype=np.float32)
    hdu = fits.PrimaryHDU(data=pixels)
    import io

    buf = io.BytesIO()
    fits.HDUList([hdu]).writeto(buf)

    frame = Frame.from_fits_bytes(buf.getvalue())
    assert frame.exposure_seconds == 0.0


def test_from_fits_bytes_raises_value_error_on_garbage() -> None:
    import pytest

    with pytest.raises(ValueError, match="Cannot parse FITS data"):
        Frame.from_fits_bytes(b"not a fits file")
