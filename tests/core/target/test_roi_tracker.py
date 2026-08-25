import pytest
from astrotool_core.target.point_source import PointSource
from astrotool_core.target.roi_tracker import RoiTracker, TrackingState


def _source(x: float, y: float, *, peak: float = 1000.0, kind: str = "normal_star") -> PointSource:
    return PointSource(x=x, y=y, peak=peak, area=20, kind=kind)


def test_update_before_acquire_raises() -> None:
    tracker = RoiTracker()
    with pytest.raises(RuntimeError):
        tracker.update([_source(10.0, 10.0)])


def test_acquire_sets_locked_state_at_the_given_position() -> None:
    tracker = RoiTracker()
    result = tracker.acquire(100.0, 200.0)
    assert result.state is TrackingState.LOCKED
    assert result.x == 100.0
    assert result.y == 200.0
    assert tracker.state is TrackingState.LOCKED


def test_nearby_source_within_tolerance_keeps_locked_and_updates_position() -> None:
    tracker = RoiTracker(lock_tolerance_px=8.0)
    tracker.acquire(100.0, 100.0)
    result = tracker.update([_source(102.0, 101.0)])
    assert result.state is TrackingState.LOCKED
    assert result.x == 102.0
    assert result.y == 101.0


def test_no_nearby_source_after_locked_becomes_lost() -> None:
    tracker = RoiTracker(lock_tolerance_px=8.0)
    tracker.acquire(100.0, 100.0)
    result = tracker.update([_source(500.0, 500.0)])
    assert result.state is TrackingState.LOST
    # last-known position is preserved while lost
    assert result.x == 100.0
    assert result.y == 100.0


def test_the_doc_example_transition_sequence() -> None:
    """Reproduces the architecture doc's own example:
    [LOCKED, LOCKED, LOST, SEARCHING, REACQUIRED].
    """
    tracker = RoiTracker(lock_tolerance_px=8.0, search_radius_px=40.0, lost_to_searching_frames=1)
    tracker.acquire(100.0, 100.0)

    frames = [
        [_source(101.0, 100.0)],  # small drift, still within tolerance -> LOCKED
        [_source(103.0, 99.0)],  # still within tolerance -> LOCKED
        [_source(500.0, 500.0)],  # star gone -> LOST
        [_source(500.0, 500.0)],  # still gone -> SEARCHING
        [_source(125.0, 90.0)],  # reappears within search radius -> REACQUIRED
    ]
    states = [tracker.update(sources).state for sources in frames]

    assert states == [
        TrackingState.LOCKED,
        TrackingState.LOCKED,
        TrackingState.LOST,
        TrackingState.SEARCHING,
        TrackingState.REACQUIRED,
    ]


def test_reacquired_then_matching_again_returns_to_locked() -> None:
    tracker = RoiTracker(lock_tolerance_px=8.0, search_radius_px=40.0, lost_to_searching_frames=1)
    tracker.acquire(100.0, 100.0)
    tracker.update([_source(500.0, 500.0)])  # LOST
    tracker.update([_source(500.0, 500.0)])  # SEARCHING
    reacquired = tracker.update([_source(125.0, 90.0)])
    assert reacquired.state is TrackingState.REACQUIRED

    locked_again = tracker.update([_source(126.0, 91.0)])
    assert locked_again.state is TrackingState.LOCKED


def test_searching_stays_searching_with_no_match_in_range() -> None:
    tracker = RoiTracker(lock_tolerance_px=8.0, search_radius_px=40.0, lost_to_searching_frames=1)
    tracker.acquire(100.0, 100.0)
    tracker.update([_source(500.0, 500.0)])  # LOST
    result = tracker.update([])  # SEARCHING, still nothing
    assert result.state is TrackingState.SEARCHING
    result = tracker.update([_source(9000.0, 9000.0)])  # far outside search radius
    assert result.state is TrackingState.SEARCHING


def test_update_reports_matched_source() -> None:
    tracker = RoiTracker(lock_tolerance_px=8.0)
    tracker.acquire(100.0, 100.0)
    source = _source(101.0, 99.0)
    result = tracker.update([source])
    assert result.matched_source == source


def test_picks_nearest_of_multiple_candidates_within_tolerance() -> None:
    tracker = RoiTracker(lock_tolerance_px=20.0)
    tracker.acquire(100.0, 100.0)
    far = _source(115.0, 100.0)
    near = _source(103.0, 100.0)
    result = tracker.update([far, near])
    assert result.matched_source == near
