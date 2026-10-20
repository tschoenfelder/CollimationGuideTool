"""S6.8b: Astronomical Mount Align with guide pulses -- the application rules, Qt-free."""

from __future__ import annotations

import threading

import pytest
from astrotool_core.mount import (
    AxisDirection,
    CalibrationUnit,
    GuidePulseResult,
    MountAxis,
    MountCapabilities,
)
from astrotool_core.mount.operating_mode import OperatingMode
from collimation_tool.application.mount_align_guide import (
    GUIDE_PULSE_MODE_CHANGED,
    GUIDE_PULSE_NOT_TRACKING,
    GUIDE_PULSE_UNSUPPORTED,
    guide_pulse_refusal,
    send_guide_pulse_move,
    unit_mismatch,
)
from collimation_tool.application.mount_motion import (
    MountAlignPrimitive,
    choose_mount_align_primitive,
)

_GUIDE = MountCapabilities(
    supports_pulse_guiding=False,
    min_pulse_ms=0,
    max_pulse_ms=0,
    supports_guide_pulses_while_tracking=True,
)
_NO_GUIDE = MountCapabilities(supports_pulse_guiding=False, min_pulse_ms=0, max_pulse_ms=0)


class _Mount:
    def __init__(self, results: list[GuidePulseResult] | None = None) -> None:
        self.calls: list[int] = []
        self.results = results or []
        self.guide_pulse_range_ms: tuple[int, int] | None = (20, 5000)

    def capabilities(self) -> MountCapabilities:
        return _GUIDE

    def guide_pulse(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int
    ) -> GuidePulseResult:
        self.calls.append(duration_ms)
        return self.results.pop(0) if self.results else GuidePulseResult(accepted=True)


@pytest.mark.parametrize(
    ("mode", "tracking", "caps", "expected"),
    [
        (OperatingMode.ASTRONOMICAL, True, _GUIDE, MountAlignPrimitive.GUIDE_PULSE),
        (OperatingMode.ASTRONOMICAL, False, _GUIDE, MountAlignPrimitive.AXIS),
        (OperatingMode.ASTRONOMICAL, None, _GUIDE, MountAlignPrimitive.AXIS),
        (OperatingMode.ASTRONOMICAL, True, _NO_GUIDE, MountAlignPrimitive.AXIS),
        (OperatingMode.TERRESTRIAL, True, _GUIDE, MountAlignPrimitive.AXIS),
        (None, True, _GUIDE, MountAlignPrimitive.AXIS),
    ],
)
def test_guide_pulses_only_in_astronomical_mode_while_tracking_with_the_capability(
    mode: OperatingMode | None,
    tracking: bool | None,
    caps: MountCapabilities,
    expected: MountAlignPrimitive,
) -> None:
    assert choose_mount_align_primitive(mode, tracking, caps) is expected


def test_a_matrix_never_sizes_moves_of_the_other_primitive() -> None:
    assert unit_mismatch(CalibrationUnit.AXIS_MS, MountAlignPrimitive.AXIS) is None
    assert unit_mismatch(CalibrationUnit.GUIDE_PULSE_MS, MountAlignPrimitive.GUIDE_PULSE) is None
    for unit, primitive in (
        (CalibrationUnit.AXIS_MS, MountAlignPrimitive.GUIDE_PULSE),
        (CalibrationUnit.GUIDE_PULSE_MS, MountAlignPrimitive.AXIS),
    ):
        text = unit_mismatch(unit, primitive)
        assert text is not None and "recalibrate in this mode" in text
        assert "nothing was sent" in text


def test_the_worker_check_refuses_on_a_fresh_reading() -> None:
    mount = _Mount()
    astro = OperatingMode.ASTRONOMICAL
    assert guide_pulse_refusal(astro, parked=False, tracking=True, mount=mount) is None
    assert (
        guide_pulse_refusal(OperatingMode.TERRESTRIAL, parked=False, tracking=True, mount=mount)
        == GUIDE_PULSE_MODE_CHANGED
    )
    assert guide_pulse_refusal(astro, parked=True, tracking=True, mount=mount) == (
        GUIDE_PULSE_NOT_TRACKING
    )
    assert guide_pulse_refusal(astro, parked=False, tracking=None, mount=mount) == (
        GUIDE_PULSE_NOT_TRACKING
    )


def test_a_failed_pulse_is_final_and_a_lost_tracking_is_said() -> None:
    mount = _Mount(
        [
            GuidePulseResult(accepted=True),
            GuidePulseResult(accepted=False, message="limit", sent=True, tracking_off=True),
        ]
    )
    result, sent = send_guide_pulse_move(
        mount,
        MountAxis.AXIS1,
        AxisDirection.POSITIVE,
        15000,
        threading.Event(),
    )
    assert not result.accepted and sent == 2
    assert mount.calls == [5000, 5000]  # the third pulse is never sent
    assert "limit" in result.message and "NOT tracking" in result.message


def test_a_move_below_the_shortest_pulse_or_without_a_range_sends_nothing() -> None:
    mount = _Mount()
    result, sent = send_guide_pulse_move(
        mount, MountAxis.AXIS2, AxisDirection.NEGATIVE, 10, threading.Event()
    )
    assert not result.accepted and sent == 0 and "nothing was sent" in result.message
    mount.guide_pulse_range_ms = None
    result, sent = send_guide_pulse_move(
        mount, MountAxis.AXIS2, AxisDirection.NEGATIVE, 1000, threading.Event()
    )
    assert result.message == GUIDE_PULSE_UNSUPPORTED and sent == 0 and mount.calls == []
