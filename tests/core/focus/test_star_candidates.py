"""Issue #58: `find_star_candidates` makes ONE candidate of every star blob and measures its
half-flux diameter (HFD).

The synthetic blobs are built like the real ones of diagnostic 4c5677d0 (a Gaussian far above
full scale, clipped at 65520, on a noisy background). Expected sizes are known by construction:
a Gaussian's HFD equals its FWHM, and more defocus (a wider star or donut of the same flux) must
give a larger HFD."""

from __future__ import annotations

import numpy as np
import pytest
from astrotool_core.focus.star_candidates import (
    StarCandidate,
    find_star_candidates,
    frame_statistics,
    is_clipped,
)
from astrotool_core.target.detector import detect_sources
from astrotool_core.testing.frame_factory import StarSpec, single_star_image, star_field_image

_FULL_SCALE = 65520.0
_SIGMA_PER_FWHM = 1.0 / 2.3548


def over_exposed(
    shape: tuple[int, int],
    centres: list[tuple[float, float]],
    *,
    amplitude: float = 2.0e6,
    sigma: float = 25.0,
    background: float = 1000.0,
    noise: float = 30.0,
    seed: int = 58,
) -> np.ndarray:
    """Gaussian blobs `amplitude` (>> full scale) high, clipped like a 16-bit sensor."""
    rng = np.random.default_rng(seed)
    image = star_field_image(
        shape,
        [StarSpec(x=x, y=y, peak=amplitude, sigma=sigma) for x, y in centres],
        background=background,
    ).astype(np.float64)
    image += rng.normal(0.0, noise, shape)
    return np.clip(np.round(image), 0.0, _FULL_SCALE)


def _gaussian(fwhm: float, flux: float, seed: int = 1) -> np.ndarray:
    sigma = fwhm * _SIGMA_PER_FWHM
    size = int(max(64, 8 * sigma + 40))
    peak = flux / (2.0 * np.pi * sigma**2)
    centre = (size - 1) / 2.0 + 0.3
    return over_exposed((size, size), [(centre, centre)], amplitude=peak, sigma=sigma, seed=seed)


def _donut(diameter: float, flux: float, seed: int = 1) -> np.ndarray:
    """A defocused C8 star: a soft-edged annulus, central obstruction 34 % of the diameter."""
    rng = np.random.default_rng(seed)
    size = int(diameter + 60)
    yy, xx = np.indices((size, size), dtype=np.float64)
    centre = (size - 1) / 2.0 + 0.3
    radius = np.hypot(xx - centre, yy - centre)
    outer, inner = diameter / 2.0, 0.34 * diameter / 2.0
    ring = 1.0 / (1.0 + np.exp((radius - outer) / 1.5))
    ring *= 1.0 / (1.0 + np.exp((inner - radius) / 1.5))
    image = 1000.0 + ring * (flux / ring.sum()) + rng.normal(0.0, 30.0, (size, size))
    return np.clip(np.round(image), 0.0, _FULL_SCALE)


def _only(frame: np.ndarray) -> StarCandidate:
    candidates = find_star_candidates(frame)
    assert len(candidates) == 1, candidates
    return candidates[0]


class TestOneBlobIsOneCandidate:
    def test_a_single_over_exposed_blob_is_one_saturated_candidate_at_its_centre(self) -> None:
        frame = over_exposed((400, 400), [(210.0, 190.0)])
        assert len(detect_sources(frame).sources) > 10  # the library fragments it

        blob = _only(frame)

        assert blob.saturated
        assert abs(blob.x - 210.0) < 2.0 and abs(blob.y - 190.0) < 2.0

    @pytest.mark.parametrize("sigma", [12.0, 16.0, 20.0])
    def test_a_clipped_star_detect_sources_drops_is_found(self, sigma: float) -> None:
        # Review F1: < 25000 bright pixels -> component mode, which drops components over
        # 5000 px: detect_sources finds nothing at all.
        frame = over_exposed((1080, 1440), [(700.0, 520.0)], sigma=sigma)
        assert detect_sources(frame).sources == ()

        blob = _only(frame)

        assert blob.saturated
        assert abs(blob.x - 700.0) < 2.0 and abs(blob.y - 520.0) < 2.0

    def test_one_well_exposed_star_is_one_candidate_with_its_fwhm_as_hfd(self) -> None:
        frame = single_star_image((160, 160), x=80.0, y=80.0, peak=3000.0, sigma=2.5)

        star = _only(frame)

        assert not star.saturated
        assert abs(star.x - 80.0) < 0.2 and abs(star.y - 80.0) < 0.2
        assert star.hfd_px == pytest.approx(2.3548 * 2.5, rel=0.1)


class TestSeveralStars:
    def test_separate_well_exposed_stars_stay_separate_candidates(self) -> None:
        frame = star_field_image(
            (160, 160),
            [StarSpec(x=50.0, y=80.0, peak=3000.0), StarSpec(x=110.0, y=80.0, peak=2000.0)],
        )

        candidates = find_star_candidates(frame)

        assert sorted(round(c.x) for c in candidates) == [50, 110]

    def test_four_over_exposed_blobs_are_four_candidates_even_when_two_halos_touch(
        self,
    ) -> None:
        # Like 4c5677d0: two of the blobs are so close that their halos merge (only the
        # dip between the plateaus separates them).
        centres = [(120.0, 110.0), (330.0, 120.0), (230.0, 230.0), (240.0, 330.0)]
        frame = over_exposed((440, 440), centres, sigma=16.0)

        candidates = find_star_candidates(frame)

        assert len(candidates) == 4
        assert all(c.saturated for c in candidates)
        for x, y in centres:
            assert sum(abs(c.x - x) < 3.0 and abs(c.y - y) < 3.0 for c in candidates) == 1

    def test_two_stars_merged_while_clipped_separate_once_unclipped(self) -> None:
        # Review F3: 70 px apart, sigma 25 -- their clipped plateaus overlap.
        centres = [(200.0, 220.0), (270.0, 220.0)]
        clipped = find_star_candidates(over_exposed((440, 480), centres))
        unclipped = find_star_candidates(over_exposed((440, 480), centres, amplitude=40000.0))

        assert len(clipped) == 1 and clipped[0].saturated
        assert 200.0 < clipped[0].x < 270.0  # one blob between the two stars
        # Unclipped they are two; each centroid stays within 6 px of its star although the
        # other's halo overlaps it heavily (sigma 25 at 70 px).
        xs = sorted(c.x for c in unclipped)
        assert len(xs) == 2
        assert abs(xs[0] - 200.0) < 6.0 and abs(xs[1] - 270.0) < 6.0


class TestHalfFluxDiameterIsMonotone:
    """Review F2: the size keeps growing with the star from ~3 to ~150 px -- the old moment
    FWHM in a 37x37 window topped out at ~24 px (true 37.7 -> 16.5, 94.2 -> 24.8)."""

    _FWHMS = [3.0, 5.0, 8.0, 12.0, 18.0, 24.0, 32.0, 48.0, 64.0, 96.0, 128.0, 150.0]
    _DIAMETERS = [6.0, 10.0, 16.0, 24.0, 40.0, 60.0, 90.0, 120.0, 150.0]

    def test_unclipped_gaussians(self) -> None:
        sizes = []
        for fwhm in self._FWHMS:
            star = _only(_gaussian(fwhm, flux=2.0e7))
            assert star.hfd_px is not None
            if fwhm >= 18.0:
                assert not star.saturated
            if fwhm <= 64.0 and not star.saturated:
                assert star.hfd_px == pytest.approx(fwhm, rel=0.1)
            sizes.append(star.hfd_px)
        assert all(b > a for a, b in zip(sizes, sizes[1:], strict=False)), sizes

    def test_clipped_tops(self) -> None:
        sizes = [_only(_gaussian(fwhm, flux=2.0e8)).hfd_px for fwhm in self._FWHMS]
        assert all(
            b is not None and a is not None and b > a
            for a, b in zip(sizes, sizes[1:], strict=False)
        ), sizes

    @pytest.mark.parametrize("flux", [2.0e7, 2.0e8])
    def test_donuts_with_a_central_obstruction(self, flux: float) -> None:
        sizes = [_only(_donut(d, flux)).hfd_px for d in self._DIAMETERS]
        assert all(
            b is not None and a is not None and b > a
            for a, b in zip(sizes, sizes[1:], strict=False)
        ), sizes


class TestDonutCentre:
    @pytest.mark.parametrize("flux", [2.0e7, 2.0e8])
    def test_a_donut_is_centred_on_its_ring(self, flux: float) -> None:
        # Review L4: the centroid came from the first grown region only (a clipped donut
        # was 4 px off); now it is the bright core of the whole blob.
        frame = _donut(60.0, flux)
        centre = (frame.shape[0] - 1) / 2.0 + 0.3

        blob = _only(frame)

        assert abs(blob.x - centre) < 1.0 and abs(blob.y - centre) < 1.0


class TestCrops:
    def test_a_crop_measures_exactly_like_the_whole_frame(self) -> None:
        frame = over_exposed((600, 800), [(300.0, 260.0), (620.0, 400.0)], amplitude=40000.0)
        stats = frame_statistics(frame)
        whole = find_star_candidates(frame, stats=stats, binning=2)
        target = min(whole, key=lambda c: abs(c.x - 300.0))

        crop = find_star_candidates(
            frame[100:420, 120:480],
            stats=stats,
            binning=2,
            origin=(120, 100),
            frame_shape=frame.shape,
        )

        assert len(crop) == 1
        assert crop[0].x == pytest.approx(target.x, abs=0.05)
        assert crop[0].hfd_px == pytest.approx(target.hfd_px, rel=0.01)
        assert not crop[0].truncated


class TestHotPixels:
    """Review N1: single hot/warm pixels and small hot clusters are never candidates and never
    make a frame clipped."""

    @pytest.mark.parametrize("value", [3000.0, 20000.0, 65520.0])
    def test_a_warm_or_hot_pixel_is_not_a_candidate(self, value: float) -> None:
        frame = over_exposed((400, 400), [], amplitude=0.0)
        frame[200, 200] = value

        assert find_star_candidates(frame) == ()

    def test_a_hot_2x2_cluster_is_not_a_candidate(self) -> None:
        frame = over_exposed((400, 400), [], amplitude=0.0)
        frame[200:202, 200:202] = _FULL_SCALE

        assert find_star_candidates(frame) == ()

    def test_300_hot_pixels_are_no_candidates_and_do_not_make_the_frame_clipped(self) -> None:
        frame = over_exposed((600, 800), [], amplitude=0.0)
        rng = np.random.default_rng(3)
        frame[rng.integers(0, 600, 300), rng.integers(0, 800, 300)] = _FULL_SCALE

        assert find_star_candidates(frame) == ()
        assert not is_clipped(frame, 65535.0)

    def test_a_clipped_star_still_makes_the_frame_clipped(self) -> None:
        assert is_clipped(over_exposed((400, 400), [(200.0, 200.0)], sigma=6.0), 65535.0)

    def test_a_hot_pixel_beside_a_star_leaves_the_star_unsaturated(self) -> None:
        frame = over_exposed((400, 400), [(200.0, 200.0)], amplitude=20000.0, sigma=4.0)
        frame[230, 200] = _FULL_SCALE

        star = _only(frame)

        assert not star.saturated
        assert abs(star.x - 200.0) < 0.5 and abs(star.y - 200.0) < 0.5


class TestOneScaleForFrameAndCrop:
    def test_a_crop_uses_the_whole_frames_threshold_beside_a_bright_distant_star(
        self,
    ) -> None:
        # Review N2: the detection threshold's 0.1 %-of-brightest term came from the analysed
        # area -- the whole frame in acquire(), the crop in measure() -- so one frame gave two
        # sizes (+4.8 % for a faint wide star, close to the searcher's 5 % margin).
        frame = over_exposed((600, 800), [(650.0, 150.0)], amplitude=2.0e6, sigma=5.0, noise=5.0)
        frame += over_exposed(
            (600, 800), [(250.0, 350.0)], amplitude=400.0, sigma=15.0, noise=5.0, seed=9
        )
        frame -= 1000.0  # one background
        stats = frame_statistics(frame, binning=1)
        whole = min(find_star_candidates(frame, stats=stats), key=lambda c: abs(c.x - 250.0))

        crop = find_star_candidates(
            frame[200:500, 100:400], stats=stats, origin=(100, 200), frame_shape=frame.shape
        )

        assert len(crop) == 1
        assert crop[0].hfd_px == pytest.approx(whole.hfd_px, rel=1e-9)


class TestCameraFullScale:
    def test_a_known_16_bit_full_scale_keeps_a_dim_star_unsaturated(self) -> None:
        # Review N3: frame maximum < 4096 -> the inferred full scale is 4095.
        frame = np.minimum(
            over_exposed((300, 300), [(150.0, 150.0)], amplitude=3060.0, sigma=3.0), 4090.0
        )

        assert _only(frame).saturated  # inferred: a 16-bit frame read as 12-bit
        known = frame_statistics(frame, full_scale=65535.0)
        assert not find_star_candidates(frame, stats=known)[0].saturated

    def test_a_12_bit_camera_clipped_at_4095_is_saturated(self) -> None:
        frame = over_exposed(
            (300, 300), [(150.0, 150.0)], amplitude=2.0e4, sigma=6.0, background=100.0
        )
        frame = np.minimum(frame, 4095.0)

        stats = frame_statistics(frame, full_scale=4095.0)

        assert find_star_candidates(frame, stats=stats)[0].saturated
        assert is_clipped(frame, 4095.0)


class TestHalfFluxDiameterIsContinuous:
    def test_small_pixel_centred_stars_grow_smoothly(self) -> None:
        # Review L2: counting each pixel at its centre radius gave steps (the same 4.0 for
        # different widths); every 0.05 px of width must now show.
        sizes = [
            _only(single_star_image((64, 64), x=32.0, y=32.0, peak=5000.0, sigma=s)).hfd_px
            for s in np.arange(0.9, 2.0, 0.05)
        ]
        assert all(
            b is not None and a is not None and b > a
            for a, b in zip(sizes, sizes[1:], strict=False)
        ), sizes
