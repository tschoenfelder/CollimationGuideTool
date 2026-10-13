"""S6.6b: Mount Align's tracking precondition.

M02 (S6.4 finding): ONE tracking-verification path -- every check goes through the
`TrackingEnforcer` (its evidence trail, its fail-closed rules). Before, a panel without an
enforcer (and a panel whose park mount read unavailable) called `ensure_tracking_mode`
directly, a second path with its own wording and no evidence.

Stop wiring (S6.3 follow-up): the capture worker hands its cancel event to the enforcer, so a
Stop ends a tracking repair's settle poll at once instead of after its timeout."""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
import pytest
from astrotool_core.mount import TrackingMode
from astrotool_core.mount.operating_mode import OperatingMode, TrackingEnforcer, TrackingGate
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.timing import FakeClock
from collimation_tool.application.mount_align_capture import CaptureJob
from collimation_tool.ui import mount_test_move_panel as panel_module
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel


def _frame() -> np.ndarray:
    return np.full((40, 60), 100.0, dtype=np.float32)


def _panel(enforcer: TrackingEnforcer | None = None) -> MountTestMovePanel:
    clock = FakeClock()
    return MountTestMovePanel(
        FakeMountAdapter(),
        mount_park=FakeMountPark(start_parked=False),
        get_left_frame=_frame,
        get_right_frame=_frame,
        tracking_enforcer=enforcer,
        clock=clock,
    )


def test_a_standalone_panel_verifies_tracking_through_the_enforcer(
    qapp: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def second_path(*_args: object, **_kwargs: object) -> Any:  # noqa: ANN401
        raise AssertionError("Mount Align must not verify tracking outside the enforcer")

    monkeypatch.setattr(panel_module, "ensure_tracking_mode", second_path, raising=False)
    panel = _panel()

    assert panel._tracking_failure_message(TrackingMode.ON, "star") is None
    assert panel._tracking_enforcer.evidence()["transitions"]  # the one path left evidence
    panel.stop()


def test_an_unavailable_mount_still_fails_the_precondition(qapp: object) -> None:
    panel = _panel()
    panel._mount_park.status = lambda: type(  # type: ignore[method-assign]
        "S", (), {"available": False, "fresh": True}
    )()

    message = panel._tracking_failure_message(TrackingMode.ON, "star")

    assert message is not None and "unavailable" in message
    panel.stop()


class _RecordingEnforcer(TrackingEnforcer):
    def __init__(self, park: FakeMountPark) -> None:
        super().__init__(park, OperatingMode.ASTRONOMICAL, settle_timeout_s=0, clock=FakeClock())
        self.cancels: list[threading.Event | None] = []

    def verify(
        self,
        required: TrackingMode,
        context: str,
        *,
        fresh_wait_s: float = 0.0,
        cancel: threading.Event | None = None,
    ) -> TrackingGate:
        self.cancels.append(cancel)
        return TrackingGate(False, "recorded")


def test_the_capture_worker_passes_its_stop_event_to_the_enforcer(qapp: object) -> None:
    park = FakeMountPark(start_parked=False)
    enforcer = _RecordingEnforcer(park)
    panel = MountTestMovePanel(
        FakeMountAdapter(),
        mount_park=park,
        get_left_frame=_frame,
        get_right_frame=_frame,
        tracking_enforcer=enforcer,
        threaded_captures=True,
        clock=FakeClock(),
    )
    request = panel._snapshot_capture_request("star", "probe", None, True)
    job = CaptureJob(1, "probe", request, lambda _m: None, 0.0)

    panel._run_capture_job(job)  # what the worker thread runs

    assert job.error is None
    assert enforcer.cancels == [job.cancel]
    panel.stop()
