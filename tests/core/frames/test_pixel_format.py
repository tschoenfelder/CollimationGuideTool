import numpy as np
import pytest
from astrotool_core.frames.pixel_format import BayerPattern, demosaic, is_bayer, mosaic_from_rgb


def _solid_bayer(pattern: BayerPattern, r: float, g: float, b: float) -> np.ndarray:
    """Build a 4x4 mosaic that is a solid (r, g, b) color under ``pattern``."""
    mosaic = np.zeros((4, 4), dtype=np.float32)
    positions = {
        BayerPattern.RGGB: {"r": (0, 0), "g1": (0, 1), "g2": (1, 0), "b": (1, 1)},
        BayerPattern.BGGR: {"b": (0, 0), "g1": (0, 1), "g2": (1, 0), "r": (1, 1)},
        BayerPattern.GRBG: {"g1": (0, 0), "r": (0, 1), "b": (1, 0), "g2": (1, 1)},
        BayerPattern.GBRG: {"g1": (0, 0), "b": (0, 1), "r": (1, 0), "g2": (1, 1)},
    }[pattern]
    for tag, (dy, dx) in positions.items():
        value = {"r": r, "g1": g, "g2": g, "b": b}[tag]
        mosaic[dy::2, dx::2] = value
    return mosaic


class TestIsBayer:
    def test_mono_is_not_bayer(self) -> None:
        assert is_bayer(BayerPattern.MONO) is False

    @pytest.mark.parametrize(
        "pattern",
        [BayerPattern.RGGB, BayerPattern.BGGR, BayerPattern.GRBG, BayerPattern.GBRG],
    )
    def test_bayer_patterns_are_bayer(self, pattern: BayerPattern) -> None:
        assert is_bayer(pattern) is True


class TestDemosaic:
    def test_mono_passthrough_returns_same_shape_rgb(self) -> None:
        plane = np.full((6, 6), 500.0, dtype=np.float32)
        rgb = demosaic(plane, BayerPattern.MONO)
        assert rgb.shape == (6, 6, 3)
        assert np.allclose(rgb[..., 0], plane)
        assert np.allclose(rgb[..., 1], plane)
        assert np.allclose(rgb[..., 2], plane)

    @pytest.mark.parametrize(
        "pattern",
        [BayerPattern.RGGB, BayerPattern.BGGR, BayerPattern.GRBG, BayerPattern.GBRG],
    )
    def test_solid_color_demosaics_to_uniform_rgb(self, pattern: BayerPattern) -> None:
        mosaic = _solid_bayer(pattern, r=1000.0, g=2000.0, b=3000.0)
        rgb = demosaic(mosaic, pattern)

        assert rgb.shape == (4, 4, 3)
        # Interior pixels (away from the mosaic edge) should reconstruct
        # the exact solid color once interpolated.
        interior = rgb[1:3, 1:3, :]
        assert np.allclose(interior[..., 0], 1000.0, atol=1.0)
        assert np.allclose(interior[..., 1], 2000.0, atol=1.0)
        assert np.allclose(interior[..., 2], 3000.0, atol=1.0)

    def test_demosaic_rejects_odd_dimensions(self) -> None:
        mosaic = np.zeros((5, 4), dtype=np.float32)
        with pytest.raises(ValueError, match="even"):
            demosaic(mosaic, BayerPattern.RGGB)

    def test_demosaic_output_dtype_is_float32(self) -> None:
        mosaic = _solid_bayer(BayerPattern.RGGB, r=10.0, g=20.0, b=30.0)
        rgb = demosaic(mosaic, BayerPattern.RGGB)
        assert rgb.dtype == np.float32


class TestMosaicFromRgb:
    def test_mono_returns_channel_average(self) -> None:
        rgb = np.zeros((4, 4, 3), dtype=np.float32)
        rgb[..., 0] = 10.0
        rgb[..., 1] = 20.0
        rgb[..., 2] = 30.0
        mosaic = mosaic_from_rgb(rgb, BayerPattern.MONO)
        assert np.allclose(mosaic, 20.0)

    @pytest.mark.parametrize(
        "pattern",
        [BayerPattern.RGGB, BayerPattern.BGGR, BayerPattern.GRBG, BayerPattern.GBRG],
    )
    def test_roundtrip_through_demosaic_reconstructs_solid_color(
        self, pattern: BayerPattern
    ) -> None:
        rgb_in = np.zeros((6, 6, 3), dtype=np.float32)
        rgb_in[..., 0] = 1000.0
        rgb_in[..., 1] = 2000.0
        rgb_in[..., 2] = 3000.0

        mosaic = mosaic_from_rgb(rgb_in, pattern)
        rgb_out = demosaic(mosaic, pattern)

        interior = rgb_out[2:4, 2:4, :]
        assert np.allclose(interior[..., 0], 1000.0, atol=1.0)
        assert np.allclose(interior[..., 1], 2000.0, atol=1.0)
        assert np.allclose(interior[..., 2], 3000.0, atol=1.0)

    def test_rejects_odd_dimensions(self) -> None:
        rgb = np.zeros((5, 4, 3), dtype=np.float32)
        with pytest.raises(ValueError, match="even"):
            mosaic_from_rgb(rgb, BayerPattern.RGGB)
