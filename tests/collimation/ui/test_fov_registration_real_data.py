"""Regression test against real captured frames — see
datasets/fov_registration/out_of_focus_daytime/README.md.

Distinct from test_fov_registration.py's synthetic-starfield tests: this
exercises the actual bug a real diagnostic incident reported (a
"confident" but meaningless match on out-of-focus daytime frames with no
resolved stars), using the real data rather than a constructed
approximation of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from astropy.io import fits
from astrotool_core.frames import demosaic, rgb_to_luma
from astrotool_core.frames.pixel_format import BayerPattern
from collimation_tool.ui.fov_registration import register_main_frame_in_guide_frame

_DATASET_DIR = (
    Path(__file__).resolve().parents[3] / "datasets" / "fov_registration" / "out_of_focus_daytime"
)
# The real rig's plate-scale ratio (main/guide, from ~/.SmartTScope/config.toml
# — see fov_overlay's docstring), same value MainWindow computes in production.
_REAL_RIG_APPROX_SCALE = 0.38 / 3.32


def _load_expected() -> dict[str, object]:
    expected: dict[str, object] = json.loads((_DATASET_DIR / "expected.json").read_text())
    return expected


@pytest.mark.skipif(not _DATASET_DIR.is_dir(), reason="dataset not populated")
class TestOutOfFocusDaytimeIncident:
    def test_matches_expected_result(self) -> None:
        with fits.open(_DATASET_DIR / "frames" / "main.fits") as hdul:
            main_mono = hdul[0].data.astype("float32")
        with fits.open(_DATASET_DIR / "frames" / "guide_raw.fits") as hdul:
            guide_raw = hdul[0].data.astype("float32")
        guide_mono = rgb_to_luma(demosaic(guide_raw, BayerPattern.RGGB))

        result = register_main_frame_in_guide_frame(
            main_mono, guide_mono, approx_scale=_REAL_RIG_APPROX_SCALE
        )

        expected = _load_expected()
        if expected["result"] is None:
            assert result is None, (
                "expected no confident match (see README.md) but got a result — "
                f"the out-of-focus/low-detail guard regressed: {result}"
            )
        else:
            assert result is not None
