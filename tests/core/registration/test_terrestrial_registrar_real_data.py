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

_DATASETS_DIR = Path(__file__).resolve().parents[3] / "datasets" / "fov_registration"
_DATASET_DIR = _DATASETS_DIR / "out_of_focus_daytime"
_LOW_GUIDE_DETAIL_DIR = _DATASETS_DIR / "daytime_low_guide_detail"
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


def _load_low_guide_detail_expected() -> dict[str, object]:
    expected: dict[str, object] = json.loads(
        (_LOW_GUIDE_DETAIL_DIR / "expected.json").read_text()
    )
    return expected


@pytest.mark.skipif(not _LOW_GUIDE_DETAIL_DIR.is_dir(), reason="dataset not populated")
class TestDaytimeLowGuideDetail:
    """See datasets/fov_registration/daytime_low_guide_detail/README.md --
    a live issue #29 verification capture (real Main+Guide, camera-only,
    no mount movement) that turned out to be a second real negative case:
    the Guide camera's own content, once properly demosaiced, lacks
    enough resolvable high-frequency detail for a confident match,
    confirmed not an exposure/SNR artifact (three real exposure
    combinations tried directly against the rig)."""

    def test_matches_expected_result(self) -> None:
        with fits.open(_LOW_GUIDE_DETAIL_DIR / "frames" / "main.fits") as hdul:
            main_mono = hdul[0].data.astype("float32")
        with fits.open(_LOW_GUIDE_DETAIL_DIR / "frames" / "guide_raw.fits") as hdul:
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

        expected = _load_low_guide_detail_expected()
        assert expected["result"] is None
        assert not result.ok, (
            "expected no confident match (see README.md) but got a result — "
            f"the sharpness-structure guard regressed: {result}"
        )

    def test_skipping_demosaic_produces_a_spurious_match_not_the_real_finding(self) -> None:
        """README.md's own documented artifact: treating the Guide's raw
        Bayer plane directly as mono (skipping the demosaic step every
        real caller in this project applies first) reads the mosaic's own
        per-pixel color bias as spurious high-frequency "detail" and
        produces AMBIGUOUS_MATCH instead of the correct
        INSUFFICIENT_STRUCTURE -- pinned here so a future change can't
        silently reintroduce a raw-Bayer registration path."""
        with fits.open(_LOW_GUIDE_DETAIL_DIR / "frames" / "main.fits") as hdul:
            main_mono = hdul[0].data.astype("float32")
        with fits.open(_LOW_GUIDE_DETAIL_DIR / "frames" / "guide_raw.fits") as hdul:
            guide_raw_as_mono = hdul[0].data.astype("float32")

        prior_a = OpticalPrior(
            name="main", sensor_width_px=main_mono.shape[1], sensor_height_px=main_mono.shape[0],
            pixel_scale_arcsec=_MAIN_PIXEL_SCALE_ARCSEC,
        )
        prior_b = OpticalPrior(
            name="guide", sensor_width_px=guide_raw_as_mono.shape[1],
            sensor_height_px=guide_raw_as_mono.shape[0],
            pixel_scale_arcsec=_GUIDE_PIXEL_SCALE_ARCSEC,
        )

        result = TerrestrialRegistrar().register(
            main_mono, guide_raw_as_mono, prior_a, prior_b
        )

        assert not result.ok
        assert result.status.value != "insufficient_structure", (
            "the raw-Bayer-as-mono artifact no longer reproduces -- if the "
            "sharpness guard was made robust to this, update README.md's "
            "own claim and consider whether registration should demosaic "
            "internally instead of relying on every caller to do it first"
        )
