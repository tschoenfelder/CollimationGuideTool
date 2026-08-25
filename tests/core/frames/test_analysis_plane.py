import numpy as np
from astropy.io import fits
from astrotool_core.frames.analysis_plane import build_analysis_plane
from astrotool_core.frames.frame import Frame


def _frame(pixels: np.ndarray, *, bit_depth: int = 16) -> Frame:
    return Frame(pixels=pixels, header=fits.Header(), exposure_seconds=1.0, bit_depth=bit_depth)


def test_dimensions_and_bit_depth_copied_from_frame() -> None:
    pixels = np.full((10, 20), 500.0, dtype=np.float32)
    plane = build_analysis_plane(_frame(pixels, bit_depth=16))
    assert plane.height == 10
    assert plane.width == 20
    assert plane.bit_depth == 16
    assert plane.timestamp > 0


def test_raw_is_clamped_uint16() -> None:
    pixels = np.array([[-5.0, 70000.0], [100.0, 30000.0]], dtype=np.float32)
    plane = build_analysis_plane(_frame(pixels, bit_depth=16))
    assert plane.raw.dtype == np.uint16
    assert plane.raw[0, 0] == 0
    assert plane.raw[0, 1] == 65535
    assert plane.raw[1, 1] == 30000


def test_mono_preserves_unclamped_float_values() -> None:
    pixels = np.array([[-5.0, 70000.0]], dtype=np.float32)
    plane = build_analysis_plane(_frame(pixels, bit_depth=16))
    assert plane.mono[0, 0] == -5.0
    assert plane.mono[0, 1] == 70000.0


def test_normalized_scales_to_zero_one_range_for_bit_depth() -> None:
    pixels = np.array([[0.0, 255.0]], dtype=np.float32)
    plane = build_analysis_plane(_frame(pixels, bit_depth=8))
    assert plane.normalized[0, 0] == 0.0
    assert plane.normalized[0, 1] == 1.0


def test_does_not_mutate_source_frame_pixels() -> None:
    pixels = np.full((4, 4), 100.0, dtype=np.float32)
    frame = _frame(pixels)
    plane = build_analysis_plane(frame)
    plane.mono[0, 0] = 999.0
    assert frame.pixels[0, 0] == 100.0


def test_explicit_plane_overrides_frame_pixels() -> None:
    frame_pixels = np.full((4, 4), 100.0, dtype=np.float32)
    other_plane = np.full((4, 4), 200.0, dtype=np.float32)
    plane = build_analysis_plane(_frame(frame_pixels), plane=other_plane)
    assert np.all(plane.mono == 200.0)
