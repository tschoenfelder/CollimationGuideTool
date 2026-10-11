"""S6.6a: Mount Align nudge / screen-move sizing and the one motion-submission point, moved out of
`MountTestMovePanel` unchanged. Tested directly (no panel, no MainWindow, no Qt)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pytest
from astrotool_core.config import MountAlignmentSettings
from astrotool_core.mount import AxisDirection, AxisResponse, CalibrationMatrix, MountAxis
from astrotool_core.mount.movement_sizing import CameraGeometry, rate_arcsec_per_s
from collimation_tool.application.mount_align_motion import (
    MotionStep,
    submit_axis_move,
    submit_motion_sequence,
)
from collimation_tool.application.mount_align_plan import ScreenDirection
from collimation_tool.application.mount_align_positioning import (
    cap_text,
    nudge_cap_arcsec,
    nudge_rate_arcsec_per_s,
    plan_nudge,
    plan_screen_move,
    uncalibrated_nudge_arcsec,
)
from collimation_tool.application.mount_align_sizing import angular_unit_rate

pytestmark = pytest.mark.unit

A1, A2 = MountAxis.AXIS1, MountAxis.AXIS2
POS, NEG = AxisDirection.POSITIVE, AxisDirection.NEGATIVE
_SETTINGS = MountAlignmentSettings(
    pulse_ms=500, rate_preset="6", max_nudge_pulse_ms=2000, nudge_target_fraction=0.5
)


def _matrix(px_per_ms: float = 0.1) -> CalibrationMatrix:
    """RA moves the image +x, Dec +y; `px_per_ms` in every direction."""
    shift = 1000 * px_per_ms
    vectors = {
        (A1, POS): (shift, 0.0),
        (A1, NEG): (-shift, 0.0),
        (A2, POS): (0.0, shift),
        (A2, NEG): (0.0, -shift),
    }
    return CalibrationMatrix(
        responses={
            (axis, direction): AxisResponse(axis, direction, 1000, dx, dy, px_per_ms)
            for (axis, direction), (dx, dy) in vectors.items()
        }
    )


def _nudge(**overrides: Any) -> Any:  # noqa: ANN401
    values: dict[str, Any] = {
        "settings": _SETTINGS,
        "camera_key": "left",
        "axis": A1,
        "direction": POS,
        "matrix": None,
        "peek_frame": lambda: np.zeros((100, 400)),
        "mode": "terrestrial",
        "angular_only": lambda: False,
        "uncalibrated_arcsec": lambda: 100.0,
        "floor_arcsec": lambda: 30.0,
    }
    values.update(overrides)
    return plan_nudge(**values)


def test_nudge_rate_and_cap() -> None:
    assert nudge_rate_arcsec_per_s(_SETTINGS) == rate_arcsec_per_s("6")
    assert nudge_cap_arcsec(_SETTINGS) == pytest.approx(2.0 * rate_arcsec_per_s("6"))
    odd = MountAlignmentSettings(rate_preset="not-a-preset")
    assert nudge_rate_arcsec_per_s(odd) == angular_unit_rate(odd)
    assert cap_text(_SETTINGS, False) == "2000ms"
    assert cap_text(_SETTINGS, True) == f'{nudge_cap_arcsec(_SETTINGS):.0f}"'


def test_an_uncalibrated_nudge_uses_the_frame_fraction_or_the_fixed_pulse() -> None:
    cameras = [CameraGeometry("left", 1000, 800, 0.5)]
    assert uncalibrated_nudge_arcsec(_SETTINGS, "left", lambda: cameras, lambda: None) == 250.0
    fallback = 500 * rate_arcsec_per_s("6") / 1000.0
    assert uncalibrated_nudge_arcsec(_SETTINGS, "right", lambda: cameras, lambda: None) == fallback
    assert uncalibrated_nudge_arcsec(_SETTINGS, "left", None, lambda: None) == fallback


def test_an_uncalibrated_timed_nudge_is_the_fixed_pulse_and_never_peeks() -> None:
    plan = _nudge(peek_frame=lambda: pytest.fail("no peek without a calibration"))
    assert plan.move is not None and plan.error is None
    assert (plan.move.duration_ms, plan.move.clamped, plan.move.arcsec) == (500, False, None)


def test_a_calibrated_nudge_moves_half_the_frame_and_is_clamped_to_the_cap() -> None:
    plan = _nudge(matrix=_matrix(0.1))  # 200 px at 0.1 px/ms = 2000 ms
    assert plan.move is not None and (plan.move.duration_ms, plan.move.clamped) == (2000, False)
    slow = _nudge(matrix=_matrix(0.05))  # 4000 ms -> capped
    assert slow.move is not None and (slow.move.duration_ms, slow.move.clamped) == (2000, True)


def test_calibrated_nudge_refusals_and_notices() -> None:
    assert _nudge(matrix=_matrix(0.0)).error == (
        "RA-axis measured no usable motion during calibration -- try Run Calibration again."
    )
    assert _nudge(matrix=_matrix(), peek_frame=lambda: None).error == (
        "no frame available before pulsing"
    )
    assert _nudge(matrix=_matrix(1000.0)).notice == "Main: nothing to move."


def test_an_angular_only_nudge_is_an_angle_capped_and_floored() -> None:
    unit = angular_unit_rate(_SETTINGS)
    plan = _nudge(angular_only=lambda: True)
    assert plan.move is not None and plan.move.arcsec == 100.0 and not plan.move.clamped
    assert plan.move.duration_ms == round(100.0 / unit * 1000.0)
    big = _nudge(angular_only=lambda: True, uncalibrated_arcsec=lambda: 1e6)
    assert big.move is not None and big.move.clamped
    assert big.move.arcsec == pytest.approx(nudge_cap_arcsec(_SETTINGS))
    tiny = _nudge(angular_only=lambda: True, uncalibrated_arcsec=lambda: 10.0)
    assert tiny.error == (
        'RA-axis move of 10.0" is below the mount\'s smallest angular move (30") -- '
        "nothing was sent"
    )


def _screen(**overrides: Any) -> Any:  # noqa: ANN401
    values: dict[str, Any] = {
        "settings": _SETTINGS,
        "camera_key": "right",
        "matrix": _matrix(0.1),
        "screen_direction": ScreenDirection.RIGHT,
        "fraction": 0.1,
        "frame": np.zeros((100, 400)),
        "mode": "star",
        "angular_only": lambda: False,
        "floor_arcsec": lambda: 0.0,
    }
    values.update(overrides)
    return plan_screen_move(**values)


def test_a_timed_screen_move_is_solved_from_the_matrix() -> None:
    plan = _screen()  # 40 px right = RA+ 400 ms
    assert plan.motion == [(A1, POS, 400)] and not plan.clamped and not plan.angular
    capped = _screen(fraction=10.0)
    assert capped.motion == [(A1, POS, 2000)] and capped.clamped


def test_screen_move_refusals_and_notices() -> None:
    assert _screen(frame=None).error == "no star detected before pulsing"
    assert _screen(fraction=0.0).notice == "Guide: nothing to move."


def test_an_angular_screen_move_is_refused_whole_below_the_floor() -> None:
    plan = _screen(angular_only=lambda: True)
    assert plan.angular and plan.motion is not None and len(plan.motion[0]) == 4
    refused = _screen(angular_only=lambda: True, floor_arcsec=lambda: 1e6)
    assert refused.motion is None and refused.error is not None
    assert "nothing was sent; choose a larger size" in refused.error


class _Runner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any, dict[str, object]]] = []

    def submit(
        self,
        park: object,
        mount: object,
        axis: MountAxis,
        direction: AxisDirection,
        pulse_ms: int,
        **kwargs: object,
    ) -> bool:
        self.calls.append(("submit", (axis, direction, pulse_ms), kwargs))
        return True

    def submit_sequence(
        self, park: object, mount: object, steps: Sequence[MotionStep], **kwargs: object
    ) -> bool:
        self.calls.append(("submit_sequence", list(steps), kwargs))
        return True


def test_a_move_without_an_angle_is_a_timed_pulse_else_an_angular_sequence() -> None:
    runner = _Runner()
    park: Any = object()
    mount: Any = object()
    options = {"rate_preset": "6", "park_after": False, "settle_ms": 7}
    port: Any = runner
    timed = submit_axis_move(port, park, mount, A1, POS, 300, None, rate_preset="6", settle_ms=7)
    angular = submit_axis_move(port, park, mount, A2, NEG, 300, 45.0, rate_preset="6", settle_ms=7)
    sequence = submit_motion_sequence(
        port, park, mount, [(A1, POS, 10)], rate_preset=None, settle_ms=0
    )
    assert timed and angular and sequence
    unpreset = {"rate_preset": None, "park_after": False, "settle_ms": 0}
    assert runner.calls == [
        ("submit", (A1, POS, 300), options),
        ("submit_sequence", [(A2, NEG, 300, 45.0)], options),
        ("submit_sequence", [(A1, POS, 10)], unpreset),
    ]
