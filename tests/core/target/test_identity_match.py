"""Tests for identity-preserving candidate resolution — issue #15 AC 1.6:
prefer the candidate consistent with the predicted position/previous
properties, and never silently switch when identity is ambiguous."""

from __future__ import annotations

from astrotool_core.target.identity_match import IdentityMatchStatus, resolve_identity
from astrotool_core.target.point_source import PointSource


def _source(x: float, y: float, peak: float = 1000.0, kind: str = "normal_star") -> PointSource:
    return PointSource(x=x, y=y, peak=peak, area=20, kind=kind)


class TestResolveIdentity:
    def test_a_single_candidate_near_the_prediction_is_matched(self) -> None:
        candidates = [_source(102.0, 98.0)]

        result = resolve_identity(candidates, predicted_position=(100.0, 100.0))

        assert result.status is IdentityMatchStatus.MATCHED
        assert result.source is candidates[0]

    def test_no_candidate_within_range_is_not_found(self) -> None:
        candidates = [_source(500.0, 500.0)]

        result = resolve_identity(
            candidates, predicted_position=(100.0, 100.0), max_position_error_px=25.0
        )

        assert result.status is IdentityMatchStatus.NOT_FOUND
        assert result.source is None

    def test_empty_candidate_list_is_not_found(self) -> None:
        result = resolve_identity([], predicted_position=(100.0, 100.0))
        assert result.status is IdentityMatchStatus.NOT_FOUND

    def test_two_equally_placed_and_bright_candidates_are_ambiguous(self) -> None:
        # Exactly tied distance-from-prediction and brightness -- no
        # tie-breaking evidence exists, so this must not silently pick one.
        candidates = [_source(102.0, 100.0, peak=1000.0), _source(100.0, 102.0, peak=1000.0)]

        result = resolve_identity(candidates, predicted_position=(100.0, 100.0))

        assert result.status is IdentityMatchStatus.AMBIGUOUS
        assert result.source is None

    def test_a_clearly_closer_candidate_beats_a_further_one_unambiguously(self) -> None:
        candidates = [_source(100.5, 100.5, peak=1000.0), _source(120.0, 120.0, peak=1000.0)]

        result = resolve_identity(candidates, predicted_position=(100.0, 100.0))

        assert result.status is IdentityMatchStatus.MATCHED
        assert result.source is candidates[0]

    def test_brightness_consistency_breaks_a_near_tie_in_position(self) -> None:
        # Both candidates are roughly equidistant from the prediction, but
        # only one matches the star's own previously-known brightness --
        # that consistency should let this resolve confidently rather
        # than reporting ambiguous purely on position closeness.
        candidates = [
            _source(103.0, 100.0, peak=1000.0),
            _source(97.0, 100.0, peak=50.0),
        ]

        result = resolve_identity(
            candidates, predicted_position=(100.0, 100.0), expected_peak=1000.0
        )

        assert result.status is IdentityMatchStatus.MATCHED
        assert result.source is candidates[0]

    def test_a_much_dimmer_decoy_closer_to_the_prediction_does_not_win(self) -> None:
        # The real, previously-known-bright star sits slightly further
        # from the naive predicted point than a faint, irrelevant decoy.
        candidates = [
            _source(101.0, 100.0, peak=20.0),  # decoy: closer, but implausibly dim
            _source(108.0, 100.0, peak=980.0),  # the real star: a bit further, brightness matches
        ]

        result = resolve_identity(
            candidates, predicted_position=(100.0, 100.0), expected_peak=1000.0,
            max_position_error_px=25.0,
        )

        assert result.status is IdentityMatchStatus.MATCHED
        assert result.source is candidates[1]
