from guide_tool.domain.guide_error import GuideError
from guide_tool.domain.guiding_state import GuideSourceHealth, source_state_from_error


def test_healthy_when_bad_frame_count_below_threshold() -> None:
    error = GuideError(accepted=True, centroid_x=1.0, centroid_y=1.0)
    state = source_state_from_error(
        error,
        running=True,
        latest_sequence=5,
        latest_frame_age_s=0.1,
        bad_frame_count=1,
        fallback_after_bad_frames=5,
    )
    assert state.health is GuideSourceHealth.HEALTHY


def test_transient_bad_when_bad_frame_count_reaches_threshold() -> None:
    state = source_state_from_error(
        None,
        running=True,
        latest_sequence=5,
        latest_frame_age_s=None,
        bad_frame_count=5,
        fallback_after_bad_frames=5,
    )
    assert state.health is GuideSourceHealth.TRANSIENT_BAD


def test_hard_failed_overrides_bad_frame_count() -> None:
    state = source_state_from_error(
        None,
        running=False,
        latest_sequence=5,
        latest_frame_age_s=None,
        bad_frame_count=0,
        fallback_after_bad_frames=5,
        hard_failure="stream crashed",
    )
    assert state.health is GuideSourceHealth.HARD_FAILED
    assert state.hard_failure == "stream crashed"
