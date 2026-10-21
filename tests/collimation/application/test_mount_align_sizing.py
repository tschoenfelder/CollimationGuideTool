"""S6.6a: Mount Align calibration sizing + net accounting (`CalibrationSizing`), moved out of
`MountTestMovePanel` unchanged. Tested directly (no panel, no MainWindow, no Qt).

S6.8d (R9): the timed bootstrap (`learn_rate` -> `install_rate`) and the rate preset are gone;
its tests went with it."""

from __future__ import annotations

from typing import cast

import pytest
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import (
    AxisDirection,
    AxisResponse,
    MountAxis,
    MountCapabilities,
    MountPort,
)
from astrotool_core.mount.movement_sizing import CameraGeometry
from collimation_tool.application.equivalent_duration import angular_unit_rate_arcsec_per_s
from collimation_tool.application.mount_align_failures import MeasurementFailureClass
from collimation_tool.application.mount_align_plan import (
    CALIBRATION_STEPS,
    CalibrationStep,
    CalibrationStepRole,
)
from collimation_tool.application.mount_align_sizing import (
    CalibrationSizing,
    angular_unit_rate,
    min_angular_arcsec,
    mount_is_angular_only,
    mount_supports_angular,
)

pytestmark = pytest.mark.unit

POS, NEG = AxisDirection.POSITIVE, AxisDirection.NEGATIVE
A1, A2 = MountAxis.AXIS1, MountAxis.AXIS2


class _TimedMount:
    """No angular capability (Mount Align cannot move it since S6.8d)."""

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9999)


class _AngularMount:
    """S6.6b: declares its angular moves and floor in `capabilities()` (no duck-typed property).
    S6.8d: the production contract -- no timed pulses."""

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            supports_pulse_guiding=False,
            min_pulse_ms=0,
            max_pulse_ms=0,
            supports_angular_moves=True,
            min_angular_arcsec=30.0,
        )

    def move_angular(self, *_args: object) -> None:
        raise AssertionError("sizing never moves the mount")


def _port(mount: object) -> MountPort:
    return cast(MountPort, mount)


def _sizing(mount: object, settings: MountAlignmentSettings | None = None) -> CalibrationSizing:
    chosen = settings or MountAlignmentSettings()
    return CalibrationSizing(mount=lambda: mount, settings=lambda: chosen)


def _cameras() -> list[CameraGeometry]:
    return [
        CameraGeometry("left", 1000, 800, 0.5),  # narrow: 500" wide
        CameraGeometry("right", 1000, 800, 2.0),  # wide: 2000" wide
    ]


def _response(dx: float, axis: MountAxis = A1, direction: AxisDirection = POS) -> AxisResponse:
    return AxisResponse(axis, direction, 1000, dx, 0.0, 0.0)


def test_mount_capability_helpers() -> None:
    assert not mount_supports_angular(_port(_TimedMount()))
    assert mount_supports_angular(_port(_AngularMount()))
    assert min_angular_arcsec(_port(_TimedMount())) == 0.0
    assert min_angular_arcsec(_port(_AngularMount())) == 30.0
    assert mount_is_angular_only(_port(_AngularMount()))
    assert not mount_is_angular_only(_port(_TimedMount()))


class _TimedAndAngularMount(_AngularMount):
    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9999,
            supports_angular_moves=True,
        )


def test_timed_pulses_never_make_mount_align_leave_the_angular_path() -> None:
    """S6.8d (R9): a mount that ALSO reports timed pulses (the dormant GuideTool's
    `pulse_axis`) is still moved angularly -- before, it got the timed bootstrap."""
    assert mount_is_angular_only(_port(_TimedAndAngularMount()))


class _DuckTypedOnlyMount(_AngularMount):
    """Has every angular method and a `min_angular_arcsec` attribute, but its capabilities say
    it moves only by timed pulses (e.g. an adapter whose angular API is present but disabled)."""

    min_angular_arcsec = 30.0

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9999)


class _CapabilityOnlyAngularMount(_AngularMount):
    """Reports angular-only moves with a 45" floor through `capabilities()` and nothing else."""

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(
            supports_pulse_guiding=False,
            min_pulse_ms=0,
            max_pulse_ms=0,
            supports_angular_moves=True,
            min_angular_arcsec=45.0,
        )


def test_the_reported_capabilities_decide_not_the_attributes() -> None:
    """S6.6b (#55 C02): the angular capability and its floor are read from `MountCapabilities`
    -- before, a `hasattr`/`getattr` probe answered 'angular' for the first double and 0" for
    the second."""
    assert not mount_supports_angular(_port(_DuckTypedOnlyMount()))
    assert min_angular_arcsec(_port(_DuckTypedOnlyMount())) == 0.0
    assert min_angular_arcsec(_port(_CapabilityOnlyAngularMount())) == 45.0
    assert mount_is_angular_only(_port(_CapabilityOnlyAngularMount()))
    settings = MountAlignmentSettings()
    assert angular_unit_rate(settings) == angular_unit_rate_arcsec_per_s(
        settings.calibration_center_rate_x
    )


def test_initial_state_matches_an_idle_panel() -> None:
    sizing = _sizing(_TimedMount())
    assert not sizing.angular_active and not sizing.angular_only and sizing.policy is None
    assert sizing.axis_net_pulse_ms == {A1: 0, A2: 0}
    assert sizing.axis_net_arcsec == {A1: 0.0, A2: 0.0}
    assert sizing.log == [] and sizing.bounded == {}


def test_without_optics_the_fixed_size_runs() -> None:
    settings = MountAlignmentSettings(pulse_ms=700)
    sizing = _sizing(_AngularMount(), settings)
    sizing.start(angular_only=True, cameras=None, distance_m=None)
    assert sizing.policy is None and sizing.log == []
    assert sizing.step_duration_ms(CALIBRATION_STEPS[0]) == 700
    assert sizing.step_arcsec(CALIBRATION_STEPS[0], 700) == pytest.approx(
        700 * sizing.unit_rate() / 1000.0
    )


def test_unknown_plate_scales_are_no_optics() -> None:
    sizing = _sizing(_TimedMount())
    sizing.start(
        angular_only=False, cameras=[CameraGeometry("left", 100, 100, None)], distance_m=None
    )
    assert sizing.policy is None and sizing.geometry == {}


def test_optics_seed_a_sized_run_and_log_it() -> None:
    settings = MountAlignmentSettings(calibration_rate_preset="6")
    sizing = _sizing(_TimedMount(), settings)
    sizing.start(angular_only=False, cameras=_cameras(), distance_m=None)
    assert sizing.policy is not None and sizing.log[0]["rate_preset"] is None
    assert set(sizing.geometry) == {"left", "right"}
    assert [entry["event"] for entry in sizing.log] == ["seed"]
    assert sizing.log[0]["seed_camera"] == "left"
    assert (
        sizing.axis_duration_ms[A1] == sizing.axis_duration_ms[A2] == sizing.log[0]["duration_ms"]
    )
    assert sizing.smallest_fov_key(["right", "left"]) == "left"
    assert sizing.is_probe(CALIBRATION_STEPS[0])
    assert not sizing.is_probe(CALIBRATION_STEPS[1])


def test_net_accounting_returns_the_mount() -> None:
    sizing = _sizing(_TimedMount())
    sizing.start(angular_only=False, cameras=None, distance_m=None)
    sizing.account_pulse(CALIBRATION_STEPS[0], 800)
    sizing.account_pulse(CALIBRATION_STEPS[1], 800)
    sizing.account_pulse(CALIBRATION_STEPS[2], 500)
    assert sizing.net_return(A1) == (NEG, 1100)
    sizing.account_arcsec(CALIBRATION_STEPS[0], 800, None)  # not angular: ignored
    assert sizing.axis_net_arcsec[A1] == 0.0
    sizing.reset_net()
    assert sizing.net_return(A1) == (POS, 0)


def test_angular_only_runs_are_sized_up_front_and_return_by_angle() -> None:
    mount = _AngularMount()
    sizing = _sizing(mount)
    sizing.start(angular_only=True, cameras=None, distance_m=None)
    unit = sizing.unit_rate()
    assert sizing.angular_active and sizing.angular_only
    step = CALIBRATION_STEPS[0]
    assert sizing.step_arcsec(step, 1000) == pytest.approx(unit)
    sizing.account_arcsec(step, 1000, unit)
    assert sizing.net_return(A1) == (NEG, 1000)
    axis, direction, duration_ms, *arcsec = sizing.motion_step(A1, NEG, 1000)
    assert (axis, direction, duration_ms) == (A1, NEG, 1000)
    assert arcsec == [pytest.approx(unit)]
    assert sizing.format_size(1000) == f'{unit:.0f}"'
    floor_ms = sizing.min_angular_ms()
    assert sizing.return_chunks(floor_ms - 1, 3000) == []
    chunks = sizing.return_chunks(7000, 3000)
    assert sum(chunks) == 7000 and min(chunks) >= floor_ms and max(chunks) - min(chunks) <= 1


def test_timed_return_chunks_are_capped_with_a_remainder() -> None:
    sizing = _sizing(_TimedMount())
    assert sizing.return_chunks(7000, 3000) == [3000, 3000, 1000]
    assert sizing.return_chunks(0, 3000) == []
    assert sizing.format_size(250) == "250 ms"
    assert sizing.motion_step(A2, POS, 250) == (A2, POS, 250)


def _sized() -> CalibrationSizing:
    sizing = _sizing(_TimedMount())
    sizing.start(angular_only=False, cameras=_cameras(), distance_m=None)
    return sizing


def _no_exclusion(*_args: object) -> None:
    raise AssertionError("nothing should be excluded")


def test_a_small_probe_is_resized_and_retried() -> None:
    sizing = _sized()
    before = sizing.axis_duration_ms[A1]
    retry = sizing.probe(
        CALIBRATION_STEPS[0],
        before,
        {"left": _response(10.0)},  # 1 % of the frame
        exclude_no_motion=_no_exclusion,
        exclude_bounded=_no_exclusion,
    )
    assert retry is True
    assert sizing.axis_duration_ms[A1] > before and sizing.axis_attempt[A1] == 2
    assert sizing.log[-1]["event"] == "probe" and sizing.log[-1]["action"] == "retry"


def test_an_in_band_probe_is_accepted_and_seeds_dec() -> None:
    sizing = _sized()
    sizing.axis_duration_ms[A1] = 1234
    retry = sizing.probe(
        CALIBRATION_STEPS[0],
        1234,
        {"left": _response(250.0)},  # 25 % of the frame
        exclude_no_motion=_no_exclusion,
        exclude_bounded=_no_exclusion,
    )
    assert retry is False and sizing.axis_duration_ms[A2] == 1234


def test_a_probe_that_cannot_move_the_image_is_excluded_as_no_motion() -> None:
    sizing = _sized()
    policy = sizing.policy
    assert policy is not None
    excluded: list[tuple[str, int]] = []
    responses = {"left": _response(0.0)}
    sizing.probe(
        CALIBRATION_STEPS[0],
        policy.max_duration_ms,
        responses,
        exclude_no_motion=lambda key, ms: excluded.append((key, ms)),
        exclude_bounded=_no_exclusion,
    )
    assert excluded == [("left", policy.max_duration_ms)] and responses == {}


def test_a_bounded_probe_is_excluded_with_its_class() -> None:
    sizing = _sized()
    policy = sizing.policy
    assert policy is not None
    excluded: list[tuple[str, MeasurementFailureClass, str]] = []
    sizing.probe(
        CALIBRATION_STEPS[0],
        policy.min_duration_ms,
        {"left": _response(900.0)},  # overshoot
        exclude_no_motion=_no_exclusion,
        exclude_bounded=lambda key, failure, reason: excluded.append((key, failure, reason)),
    )
    assert excluded == [("left", MeasurementFailureClass.MOVE_TOO_LARGE, "overshoot_at_minimum")]


def test_an_axis_group_returns_discarded_probes_and_queues_a_wide_followup() -> None:
    sizing = _sized()
    sizing.axis_duration_ms[A1] = 500
    sizing.account_pulse(CALIBRATION_STEPS[0], 400)  # a discarded probe left +400 ms
    last = CalibrationStep(A1, NEG, CalibrationStepRole.REPEAT)
    partial = {"left": {A1: _response(250.0)}, "right": {A1: _response(80.0)}}  # wide: 8 %
    steps = sizing.axis_group_followups(
        last,
        failed_cameras=set(),
        partial=partial,
        exclude_no_motion=_no_exclusion,
        exclude_bounded=_no_exclusion,
    )
    assert steps[0] == CalibrationStep(A1, NEG, CalibrationStepRole.RETURN, duration_ms=400)
    followups = steps[1:]
    assert [s.only_camera for s in followups] == ["right"] * 4
    assert [(s.direction, s.role) for s in followups] == [
        (POS, CalibrationStepRole.FIRST),
        (POS, CalibrationStepRole.REPEAT),
        (NEG, CalibrationStepRole.FIRST),
        (NEG, CalibrationStepRole.REPEAT),
    ]
    assert followups[0].duration_ms is not None and followups[0].duration_ms > 500
    assert sizing.log[-1]["event"] == "followup"


def test_only_the_end_of_a_sized_group_plans_anything() -> None:
    sizing = _sized()
    for step in CALIBRATION_STEPS[:3]:
        assert (
            sizing.axis_group_followups(
                step,
                failed_cameras=set(),
                partial={"left": {}, "right": {}},
                exclude_no_motion=_no_exclusion,
                exclude_bounded=_no_exclusion,
            )
            == []
        )
    unsized = _sizing(_TimedMount())
    assert (
        unsized.axis_group_followups(
            CALIBRATION_STEPS[3],
            failed_cameras=set(),
            partial={"left": {}, "right": {}},
            exclude_no_motion=_no_exclusion,
            exclude_bounded=_no_exclusion,
        )
        == []
    )
