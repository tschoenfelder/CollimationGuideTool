"""Characterization test for _detect_pixel_shift, ported byte-for-byte from
smart_telescope's adapters/touptek/managed.py. Pins its current, observed
behavior before any future change — per CONTRIBUTING.md.
"""

from __future__ import annotations

import numpy as np
from astrotool_core.camera.touptek_adapter import _detect_pixel_shift


def _msb_aligned(adc_values: np.ndarray, *, shift: int, offset: int = 0) -> np.ndarray:
    return (adc_values.astype(np.uint16) << shift) + np.uint16(offset)


def test_true_16_bit_data_needs_no_shift() -> None:
    rng = np.random.default_rng(1)
    adc = rng.integers(0, 65000, size=(64, 64), dtype=np.uint16)
    raw = _msb_aligned(adc, shift=0)
    assert _detect_pixel_shift(raw) == 0


def test_12_bit_msb_aligned_data_detects_shift_of_4() -> None:
    rng = np.random.default_rng(2)
    adc = rng.integers(1, 4095, size=(64, 64), dtype=np.uint16)
    raw = _msb_aligned(adc, shift=4)
    assert _detect_pixel_shift(raw) == 4


def test_14_bit_msb_aligned_data_detects_shift_of_2() -> None:
    rng = np.random.default_rng(3)
    adc = rng.integers(1, 16383, size=(64, 64), dtype=np.uint16)
    raw = _msb_aligned(adc, shift=2)
    assert _detect_pixel_shift(raw) == 2


def test_12_bit_data_with_nonzero_black_level_offset_still_detects_shift_of_4() -> None:
    # A black-level offset that is not a multiple of 16 would defeat a naive
    # divisibility check (see the function's docstring) — the GCD-of-diffs
    # approach must still recover shift=4.
    rng = np.random.default_rng(4)
    adc = rng.integers(1, 4095, size=(64, 64), dtype=np.uint16)
    raw = _msb_aligned(adc, shift=4, offset=5)
    assert _detect_pixel_shift(raw) == 4


def test_too_few_nonzero_pixels_returns_minus_one() -> None:
    raw = np.zeros((64, 64), dtype=np.uint16)
    raw[0, :50] = 100  # only 50 nonzero pixels, below the 100-pixel floor
    assert _detect_pixel_shift(raw) == -1


def test_too_little_variety_returns_minus_one() -> None:
    raw = np.full((64, 64), 100, dtype=np.uint16)  # only 1 distinct nonzero value
    assert _detect_pixel_shift(raw) == -1


def test_all_zero_frame_returns_minus_one() -> None:
    raw = np.zeros((64, 64), dtype=np.uint16)
    assert _detect_pixel_shift(raw) == -1
