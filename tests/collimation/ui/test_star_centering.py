"""Operator (2026-11-27): "For rough and fine collimation, at least the donut or star has to
be kept in the center, as every change of the collimation screws will shift both."

The loop runs against the production mount adapters over the fake OnStep client: the camera
sees a disc wherever the commanded mount moves have put it."""

from __future__ import annotations

import math
import time

import numpy as np
import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from collimation_tool.application.star_centering import (
    BELOW_SMALLEST_MOVE,
    MAX_MOVES,
    CenteringAction,
    StarCenterer,
)
from collimation_tool.ui.star_centering_panel import StarCenteringPanel
from test_mount_align_real_onstep_adapter import _calibrated, _Rig

_SHAPE = (540, 960)


class TestStarCenterer:
    def test_a_resting_star_off_centre_asks_for_a_move_towards_the_centre(self) -> None:
        centerer = StarCenterer()

        first = centerer.read((200.0, 100.0), _SHAPE)
        second = centerer.read((201.0, 100.0), _SHAPE)

        assert first.action is CenteringAction.WAIT
        assert second.action is CenteringAction.MOVE
        assert second.dx_px == pytest.approx(0.8 * (479.5 - 201.0))
        assert second.dy_px == pytest.approx(0.8 * (269.5 - 100.0))

    def test_a_star_still_moving_under_the_operators_hand_is_left_alone(self) -> None:
        centerer = StarCenterer()
        centerer.read((200.0, 100.0), _SHAPE)

        decision = centerer.read((240.0, 100.0), _SHAPE)

        assert decision.action is CenteringAction.WAIT

    def test_a_star_near_the_centre_is_centred(self) -> None:
        centerer = StarCenterer()
        centerer.read((490.0, 275.0), _SHAPE)

        assert centerer.read((490.0, 275.0), _SHAPE).action is CenteringAction.CENTERED

    def test_it_gives_up_when_moves_do_not_bring_the_star_in(self) -> None:
        centerer = StarCenterer()
        for _ in range(MAX_MOVES):
            centerer.read((200.0, 100.0), _SHAPE)
            assert centerer.read((200.0, 100.0), _SHAPE).action is CenteringAction.MOVE
            centerer.moved()
        centerer.read((200.0, 100.0), _SHAPE)

        assert centerer.read((200.0, 100.0), _SHAPE).action is CenteringAction.GIVE_UP

    def test_no_star_is_never_a_move(self) -> None:
        assert StarCenterer().read(None, _SHAPE).action is CenteringAction.WAIT


def _disc_frame(shape: tuple[int, int], x: float, y: float, radius: float) -> np.ndarray:
    yy, xx = np.mgrid[0 : shape[0], 0 : shape[1]]
    lit = np.hypot(xx - x, yy - y) <= radius
    noise = np.random.default_rng(0).normal(0.0, 3.0, shape)
    frame: np.ndarray = (100.0 + 3000.0 * lit + noise).astype(np.float32)
    return frame


def test_the_mount_moves_until_the_disc_sits_in_the_middle_of_main(qapp: object) -> None:
    rig = _Rig(rot_deg=35.0, dec_sign=-1.0)  # RA/Dec do not follow the frame axes
    panel = _calibrated(rig)
    height, width = rig.base["left"].shape
    shift_at_start = rig.image_shift_px("left")
    start = (width * 0.28, height * 0.30)

    def disc_position() -> tuple[float, float]:
        dx, dy = rig.image_shift_px("left")
        return (start[0] + dx - shift_at_start[0], start[1] + dy - shift_at_start[1])

    def wait(_after: float, _timeout: float) -> FrameAcquisitionResult:
        x, y = disc_position()
        return FrameAcquisitionResult(
            status=FrameAcquisitionStatus.OK,
            frame=DeliveredFrame(
                pixels=_disc_frame((height, width), x, y, height * 0.12),
                captured_at_monotonic=time.monotonic(),
                exposure_seconds=0.01,
            ),
        )

    centering = StarCenteringPanel(
        wait_for_frame_after=wait,
        cycle_s=lambda: 0.1,
        blocked=panel.centering_blocker,
        moving=lambda: panel.is_moving,
        move_main=panel.move_main_image,
    )
    centering.set_active(True)

    deadline = time.monotonic() + 60.0
    while not centering.status_text.startswith("Star centred"):
        assert time.monotonic() < deadline, centering.status_text
        assert centering.active, centering.status_text
        centering.poll()
        panel._poll()
        time.sleep(0.01)

    x, y = disc_position()
    initial = math.hypot(start[0] - width / 2, start[1] - height / 2)
    # This small rig camera ends within a few smallest mount moves of the centre.
    assert math.hypot(x - width / 2, y - height / 2) <= 0.3 * initial
    assert {axis for axis, _deg in rig.client.axis_move_calls} == {"ra", "dec"}
    centering.stop()
    panel.stop()


def test_without_a_main_calibration_nothing_moves_and_the_reason_is_shown(
    qapp: object,
) -> None:
    rig = _Rig()
    panel = rig.panel()
    centering = StarCenteringPanel(
        wait_for_frame_after=rig.waiter("left"),
        cycle_s=lambda: 0.1,
        blocked=panel.centering_blocker,
        moving=lambda: panel.is_moving,
        move_main=panel.move_main_image,
    )
    centering.set_active(True)

    centering.poll()

    assert "calibration with the Main camera" in centering.status_text
    assert panel.move_main_image(50.0, 0.0) is not None
    assert rig.client.axis_move_calls == []
    centering.stop()
    panel.stop()


def test_the_mount_align_stop_switches_centring_off(qapp: object) -> None:
    rig = _Rig()
    panel = rig.panel()
    centering = StarCenteringPanel(
        wait_for_frame_after=rig.waiter("left"),
        cycle_s=lambda: 0.1,
        blocked=lambda: None,
        moving=lambda: False,
        move_main=panel.move_main_image,
    )
    panel.stop_requested.connect(centering.stop)
    centering.set_active(True)

    panel._on_stop()

    assert not centering.active
    panel.stop()


def test_a_failed_centring_move_switches_centring_off_and_says_why(qapp: object) -> None:
    rig = _Rig()
    centering = StarCenteringPanel(
        wait_for_frame_after=rig.waiter("left"),
        cycle_s=lambda: 0.1,
        blocked=lambda: None,
        moving=lambda: False,
        move_main=lambda _dx, _dy: None,
    )
    centering.set_active(True)

    centering.fail("limit reached")

    assert not centering.active
    assert "limit reached" in centering.status_text


@pytest.mark.parametrize(("x", "expected_active"), [(380.0, True), (100.0, False)])
def test_a_correction_below_the_smallest_move_counts_as_centred_only_when_near(
    qapp: object, x: float, expected_active: bool
) -> None:
    frame = _disc_frame(_SHAPE, x, 270.0, 40.0)

    def wait(_after: float, _timeout: float) -> FrameAcquisitionResult:
        return FrameAcquisitionResult(
            status=FrameAcquisitionStatus.OK,
            frame=DeliveredFrame(
                pixels=frame, captured_at_monotonic=time.monotonic(), exposure_seconds=0.01
            ),
        )

    centering = StarCenteringPanel(
        wait_for_frame_after=wait,
        cycle_s=lambda: 0.1,
        blocked=lambda: None,
        moving=lambda: False,
        move_main=lambda _dx, _dy: BELOW_SMALLEST_MOVE,
    )
    centering.set_active(True)
    deadline = time.monotonic() + 30.0
    while centering.active and not centering.status_text.startswith(("Star centred", "Cent")):
        assert time.monotonic() < deadline, centering.status_text
        centering.poll()
        time.sleep(0.01)

    assert centering.active is expected_active, centering.status_text
    centering.stop()
