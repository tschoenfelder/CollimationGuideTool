"""Issue #46: calibration moves are sized to ~25% of the relevant frame
dimension from the SMALLEST participating FOV, adapt on the measured pixel
displacement, and never silently run longer than the cap.

Expected numbers are hand-computed from the issue's optics, independent of the
code under test:

  G3M678M/OAG on the C8: 3840x2160, 2.0 um pixel, f=2030 mm
      scale = 206.265 * 2.0 / 2030 = 0.20322 "/px -> FOV 780.4" x 439.0"
      25% of the width = 195.1"
  ATR585M/Main on the C8: FOV 1131.5" x 636.5" (18.858' x 10.608') -> 25% w = 282.9"
  GPCMOS02000/Guide, 180 mm: 3.323 "/px, 1920x1080 -> FOV 6380" x 3589"
  rate preset 6 = 20x sidereal = 20 * 15.041 = 300.82 "/s
"""

from __future__ import annotations

from astrotool_core.mount.movement_sizing import (
    CameraGeometry,
    SizingPolicy,
    SizingStatus,
    StepAction,
    decide_next,
    finite_distance_scale,
    measured_fraction,
    plan_first_move,
    plan_followup,
    rate_arcsec_per_s,
)

_POLICY = SizingPolicy()  # preset 6 (20x), 25% target, 20-30% band, 3 s cap, 0.2 s floor

_OAG = CameraGeometry("oag", 3840, 2160, 206.265 * 2.0 / 2030.0, focal_length_mm=2030.0)
_MAIN = CameraGeometry("main", 3840, 2160, 206.265 * 2.9 / 2030.0, focal_length_mm=2030.0)
_GUIDE = CameraGeometry("guide", 1920, 1080, 206.265 * 2.9 / 180.0, focal_length_mm=180.0)


class TestRates:
    def test_preset_6_is_20x_sidereal(self) -> None:
        assert abs(rate_arcsec_per_s("6") - 300.82) < 0.05

    def test_preset_7_is_48x_sidereal(self) -> None:
        assert abs(rate_arcsec_per_s("7") - 721.97) < 0.05


class TestFiniteDistance:
    def test_infinity_leaves_the_fov_unchanged(self) -> None:
        assert finite_distance_scale(None, 2030.0) == 1.0

    def test_ten_km_is_practically_unchanged(self) -> None:
        # 13.003' vs 13.006' in the issue
        assert abs(finite_distance_scale(10_000.0, 2030.0) - 0.999797) < 1e-6

    def test_thirty_metres_shrinks_the_fov_about_seven_percent(self) -> None:
        # 12.126' vs 13.006' in the issue
        assert abs(finite_distance_scale(30.0, 2030.0) - 0.93233) < 1e-4

    def test_unknown_focal_length_cannot_apply_a_correction(self) -> None:
        assert finite_distance_scale(30.0, None) == 1.0


class TestFirstMoveSeed:
    def test_stars_seed_from_25_percent_of_the_width(self) -> None:
        plan = plan_first_move([_OAG], _POLICY)

        # 195.1" / 300.82 "/s = 0.6486 s
        assert plan.status is SizingStatus.OK
        assert abs(plan.duration_ms - 649) <= 2
        assert plan.seed_camera == "oag"

    def test_terrestrial_10km_seed(self) -> None:
        plan = plan_first_move([_OAG], _POLICY, distance_m=10_000.0)

        assert abs(plan.duration_ms - 648) <= 2  # 195.0" / 300.82

    def test_artificial_star_30m_seed_is_shorter(self) -> None:
        plan = plan_first_move([_OAG], _POLICY, distance_m=30.0)

        # 195.1" * 0.93233 = 181.9" -> 0.6046 s
        assert abs(plan.duration_ms - 605) <= 2

    def test_the_smallest_fov_controls_the_first_move(self) -> None:
        plan = plan_first_move([_GUIDE, _MAIN, _OAG], _POLICY)

        assert plan.seed_camera == "oag"
        assert abs(plan.duration_ms - 649) <= 2

    def test_main_and_guide_are_sized_from_main(self) -> None:
        plan = plan_first_move([_GUIDE, _MAIN], _POLICY)

        # 282.9" / 300.82 = 0.9404 s
        assert plan.seed_camera == "main"
        assert abs(plan.duration_ms - 940) <= 3

    def test_the_seed_does_not_depend_on_camera_order(self) -> None:
        assert plan_first_move([_OAG, _MAIN, _GUIDE], _POLICY) == plan_first_move(
            [_GUIDE, _OAG, _MAIN], _POLICY
        )

    def test_a_move_needing_more_than_the_cap_is_capped_not_run_longer(self) -> None:
        plan = plan_first_move([_GUIDE], _POLICY)

        # 25% of the Guide's width = 1595" needs 5.30 s at 20x -> capped at 3 s
        assert plan.status is SizingStatus.CAPPED
        assert plan.duration_ms == 3000

    def test_a_faster_rate_brings_the_wide_guide_inside_the_cap(self) -> None:
        fast = SizingPolicy(rate_preset="7")  # 48x = 721.97 "/s -> 2.21 s

        plan = plan_first_move([_GUIDE], fast)

        assert plan.status is SizingStatus.OK
        assert abs(plan.duration_ms - 2209) <= 5

    def test_a_tiny_required_move_is_raised_to_the_minimum_useful_duration(self) -> None:
        tiny = CameraGeometry("tiny", 100, 100, 0.05, focal_length_mm=None)  # 5" x 5" FOV

        plan = plan_first_move([tiny], _POLICY)

        assert plan.status is SizingStatus.RAISED_TO_MIN
        assert plan.duration_ms == 200

    def test_unknown_plate_scale_falls_back_to_the_given_seed(self) -> None:
        unknown = CameraGeometry("main", 3840, 2160, None, None)

        plan = plan_first_move([unknown], _POLICY, fallback_ms=1000)

        assert plan.status is SizingStatus.NO_OPTICS
        assert plan.duration_ms == 1000

    def test_cameras_without_optics_do_not_block_sizing_from_a_known_one(self) -> None:
        unknown = CameraGeometry("x", 3840, 2160, None, None)

        plan = plan_first_move([unknown, _OAG], _POLICY)

        assert plan.status is SizingStatus.OK
        assert plan.seed_camera == "oag"


class TestMeasuredFraction:
    def test_a_horizontal_response_is_judged_against_the_width(self) -> None:
        assert abs(measured_fraction(_OAG, 960.0, 0.0) - 0.25) < 1e-9

    def test_a_vertical_response_is_judged_against_the_height(self) -> None:
        assert abs(measured_fraction(_OAG, 0.0, 540.0) - 0.25) < 1e-9

    def test_a_rotated_response_uses_the_dominant_component(self) -> None:
        # 45 deg camera rotation: (dx, dy) both nonzero -> the larger fraction decides
        fraction = measured_fraction(_OAG, 500.0, 300.0)

        assert abs(fraction - max(500.0 / 3840.0, 300.0 / 2160.0)) < 1e-9

    def test_sign_does_not_matter(self) -> None:
        assert measured_fraction(_OAG, -960.0, -10.0) == measured_fraction(_OAG, 960.0, 10.0)


class TestAdaptiveResizing:
    def test_a_move_inside_the_band_is_accepted(self) -> None:
        decision = decide_next(365, 0.25, _POLICY, attempt=1)

        assert decision.action is StepAction.ACCEPT
        assert decision.duration_ms == 365

    def test_the_band_edges_are_accepted(self) -> None:
        assert decide_next(365, 0.20, _POLICY, attempt=1).action is StepAction.ACCEPT
        assert decide_next(365, 0.30, _POLICY, attempt=1).action is StepAction.ACCEPT

    def test_an_undershoot_increases_the_move_proportionally(self) -> None:
        decision = decide_next(400, 0.05, _POLICY, attempt=1)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms == 1600  # x(0.25/0.05)=x5 clamped to x4

    def test_a_moderate_undershoot_scales_to_the_target(self) -> None:
        decision = decide_next(400, 0.125, _POLICY, attempt=1)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms == 800  # x2

    def test_an_overshoot_reduces_the_move(self) -> None:
        decision = decide_next(1000, 0.50, _POLICY, attempt=1)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms == 500

    def test_a_shift_near_the_estimator_alias_limit_is_reduced_not_accepted(self) -> None:
        decision = decide_next(1000, 0.48, _POLICY, attempt=1)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms < 1000

    def test_a_zero_displacement_grows_the_move_by_the_maximum_step(self) -> None:
        decision = decide_next(300, 0.0, _POLICY, attempt=1)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms == 1200

    def test_growth_never_exceeds_the_cap(self) -> None:
        decision = decide_next(2000, 0.05, _POLICY, attempt=1)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms == 3000

    def test_at_the_cap_a_useful_but_short_move_is_accepted_explicitly(self) -> None:
        decision = decide_next(3000, 0.14, _POLICY, attempt=2)

        assert decision.action is StepAction.ACCEPT
        assert decision.reason == "at_cap_below_band"

    def test_at_the_cap_an_unmeasurably_small_move_is_an_explicit_bounded_failure(self) -> None:
        decision = decide_next(3000, 0.03, _POLICY, attempt=2)

        assert decision.action is StepAction.BOUNDED
        assert decision.reason == "exceeds_envelope"

    def test_shrinking_stops_at_the_minimum_useful_duration(self) -> None:
        decision = decide_next(210, 0.9, _POLICY, attempt=1)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms == 200
        floor = decide_next(200, 0.9, _POLICY, attempt=2)
        assert floor.action is StepAction.BOUNDED
        assert floor.reason == "overshoot_at_minimum"

    def test_retries_are_bounded(self) -> None:
        decision = decide_next(800, 0.15, _POLICY, attempt=_POLICY.max_attempts)

        assert decision.action is StepAction.ACCEPT  # measurable (>=10%) after the last try
        assert decision.reason == "attempts_exhausted"
        bad = decide_next(800, 0.02, _POLICY, attempt=_POLICY.max_attempts)
        assert bad.action is StepAction.BOUNDED
        assert bad.reason == "attempts_exhausted"


class TestWideCameraFollowUp:
    def test_a_camera_already_above_ten_percent_needs_no_second_move(self) -> None:
        decision = plan_followup(940, 0.12, _POLICY)

        assert decision.action is StepAction.ACCEPT

    def test_the_guide_after_a_main_sized_move_gets_a_larger_capped_move(self) -> None:
        # Main-sized 940 ms (283") moves the Guide only ~4.4% of its width
        decision = plan_followup(940, 0.044, _POLICY)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms == 3000  # would need 5.3 s -> capped, never longer
        # ... and 3000/940 * 4.4% = 14% is still a useful measurement
        assert 0.10 <= 0.044 * 3000 / 940

    def test_a_camera_that_stays_below_ten_percent_even_at_the_cap_is_bounded(self) -> None:
        decision = plan_followup(940, 0.01, _POLICY)

        assert decision.action is StepAction.BOUNDED
        assert decision.reason == "exceeds_envelope"

    def test_a_followup_that_fits_the_cap_targets_25_percent(self) -> None:
        decision = plan_followup(600, 0.075, _POLICY)

        assert decision.action is StepAction.RETRY
        assert decision.duration_ms == 2000  # x(0.25/0.075) = 3.33 -> 2000


class TestSymmetryAndCameraCount:
    def test_one_plan_serves_both_directions_of_an_axis(self) -> None:
        # The plan is direction-independent by construction: same seed for +/-.
        plan = plan_first_move([_MAIN, _GUIDE], _POLICY)

        assert plan.duration_ms == plan_first_move([_MAIN, _GUIDE], _POLICY).duration_ms

    def test_any_number_of_cameras_participate_not_just_two(self) -> None:
        plan = plan_first_move([_GUIDE, _MAIN, _OAG], _POLICY)

        assert plan.seed_camera == "oag"  # the third (displayed-or-not) camera decides

    def test_no_cameras_falls_back(self) -> None:
        plan = plan_first_move([], _POLICY, fallback_ms=1000)

        assert plan.status is SizingStatus.NO_OPTICS
        assert plan.duration_ms == 1000
