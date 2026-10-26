"""Issue #58: `group_star_candidates` makes ONE candidate of every star blob.

`detect_sources` splits a heavily over-exposed star (a clipped plateau with a wide halo)
into dozens of local maxima once more than 25000 pixels are bright; the synthetic blobs here
are built like the real ones of diagnostic 4c5677d0 (a Gaussian far above full scale, clipped
at 65520, on a noisy background), so the fragmentation is the library's own, not staged."""

from __future__ import annotations

import numpy as np
from astrotool_core.focus.star_candidates import group_star_candidates
from astrotool_core.target.detector import detect_sources
from astrotool_core.testing.frame_factory import StarSpec, single_star_image, star_field_image

_FULL_SCALE = 65520.0


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


class TestOneBlobIsOneCandidate:
    def test_a_single_over_exposed_blob_is_one_saturated_candidate_at_its_centre(self) -> None:
        frame = over_exposed((400, 400), [(210.0, 190.0)])
        sources = detect_sources(frame).sources
        assert len(sources) > 10  # the library fragments it (precondition of the defect)

        candidates = group_star_candidates(frame, sources)

        assert len(candidates) == 1
        (blob,) = candidates
        assert blob.saturated
        assert blob.member_count == len(sources)
        assert abs(blob.x - 210.0) < 3.0 and abs(blob.y - 190.0) < 3.0

    def test_one_well_exposed_star_is_returned_unchanged(self) -> None:
        frame = single_star_image((160, 160), x=80.0, y=80.0, peak=3000.0, sigma=2.5)
        sources = detect_sources(frame).sources

        candidates = group_star_candidates(frame, sources)

        assert len(candidates) == 1
        assert candidates[0].source == sources[0]
        assert (candidates[0].x, candidates[0].y) == (sources[0].x, sources[0].y)
        assert candidates[0].member_count == 1


class TestSeveralStars:
    def test_separate_well_exposed_stars_stay_separate_candidates(self) -> None:
        frame = star_field_image(
            (160, 160),
            [StarSpec(x=50.0, y=80.0, peak=3000.0), StarSpec(x=110.0, y=80.0, peak=2000.0)],
        )
        sources = detect_sources(frame).sources

        candidates = group_star_candidates(frame, sources)

        assert len(candidates) == 2
        assert {c.source for c in candidates} == set(sources)

    def test_four_over_exposed_blobs_are_four_candidates_even_when_two_halos_touch(
        self,
    ) -> None:
        # Like 4c5677d0: two of the blobs are so close that their halos merge (only the
        # dip between the plateaus separates them).
        centres = [(120.0, 110.0), (330.0, 120.0), (230.0, 230.0), (240.0, 330.0)]
        frame = over_exposed((440, 440), centres, sigma=16.0)
        sources = detect_sources(frame).sources
        assert len(sources) > 4 * 5

        candidates = group_star_candidates(frame, sources)

        assert len(candidates) == 4
        assert all(c.saturated for c in candidates)
        for x, y in centres:
            assert any(abs(c.x - x) < 4.0 and abs(c.y - y) < 4.0 for c in candidates), (x, y)

    def test_every_source_belongs_to_exactly_one_candidate(self) -> None:
        frame = over_exposed((440, 440), [(120.0, 110.0), (330.0, 300.0)], sigma=18.0)
        sources = detect_sources(frame).sources

        candidates = group_star_candidates(frame, sources)

        assert len(candidates) == 2
        assert sum(c.member_count for c in candidates) <= len(sources)
        assert sum(c.member_count for c in candidates) >= 0.9 * len(sources)
