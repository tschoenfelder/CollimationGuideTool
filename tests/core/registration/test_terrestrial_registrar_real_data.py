"""Regression test against real captured frames — see
datasets/fov_registration/out_of_focus_daytime/README.md. Ported from
collimation_tool.ui's original test_fov_registration_real_data.py (issue
#29 moved the algorithm into astrotool_core.registration).

Distinct from test_terrestrial_registrar.py's synthetic-starfield tests:
this exercises the actual bug a real diagnostic incident reported (a
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
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.terrestrial_registrar import TerrestrialRegistrar

_DATASET_DIR = (
    Path(__file__).resolve().parents[3] / "datasets" / "fov_registration" / "out_of_focus_daytime"
)
# The real rig's own plate scale (arcsec/px, main/guide -- see
# fov_overlay's docstring), same values MainWindow reads in production.
_MAIN_PIXEL_SCALE_ARCSEC = 0.38
_GUIDE_PIXEL_SCALE_ARCSEC = 3.32


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

        prior_a = OpticalPrior(
            name="main", sensor_width_px=main_mono.shape[1], sensor_height_px=main_mono.shape[0],
            pixel_scale_arcsec=_MAIN_PIXEL_SCALE_ARCSEC,
        )
        prior_b = OpticalPrior(
            name="guide", sensor_width_px=guide_mono.shape[1],
            sensor_height_px=guide_mono.shape[0], pixel_scale_arcsec=_GUIDE_PIXEL_SCALE_ARCSEC,
        )

        result = TerrestrialRegistrar().register(main_mono, guide_mono, prior_a, prior_b)

        expected = _load_expected()
        if expected["result"] is None:
            assert not result.ok, (
                "expected no confident match (see README.md) but got a result — "
                f"the out-of-focus/low-detail guard regressed: {result}"
            )
        else:
            assert result.ok
