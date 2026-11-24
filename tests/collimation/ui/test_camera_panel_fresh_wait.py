"""Field bundle 4013795c (2026-11-24): the freshness wait walked the frame ring from its oldest
frame and converted each stale colour frame to mono (0.5-0.8 s each on the Pi) before checking
its exposure start, so it ran out of time before reaching the fresh frames. A stale frame must not
be converted; only the frame that passes the freshness rule is."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import FrameAcquisitionStatus
from collimation_tool.ui.camera_panel import CameraPanel


class _Mailbox:
    def __init__(self, frames: list[SimpleNamespace]) -> None:
        self._frames = frames

    def wait_next_after(self, cursor: int, *, timeout_s: float) -> SimpleNamespace | None:
        for frame in self._frames:
            if frame.sequence > cursor:
                return frame
        return None


def _mailbox_frame(sequence: int, captured_at: float) -> SimpleNamespace:
    return SimpleNamespace(
        sequence=sequence,
        captured_at_monotonic=captured_at,
        frame=SimpleNamespace(exposure_seconds=0.0003),
    )


def test_stale_frames_are_not_converted_and_the_fresh_one_is_returned() -> None:
    converted: list[float] = []
    fresh_pixels = np.ones((4, 4), dtype=np.float32)
    stale = [_mailbox_frame(seq, 10.0 + 0.3 * seq) for seq in range(1, 9)]  # 10.3 .. 12.4
    fresh = _mailbox_frame(9, 13.5)  # exposure starts after the reference (13.0)

    def to_mono(frame: Any) -> np.ndarray:  # noqa: ANN401
        converted.append(1.0)
        return fresh_pixels

    panel: Any = SimpleNamespace(
        _stream=SimpleNamespace(mailbox=_Mailbox([*stale, fresh])),
        _remember_frame=lambda _frame: None,
        _frame_to_mono=to_mono,
    )

    result = CameraPanel.wait_for_frame_after(panel, 13.0, 5.0)

    assert result.status is FrameAcquisitionStatus.OK
    assert result.frame is not None and result.frame.pixels is fresh_pixels
    assert len(converted) == 1  # only the fresh frame, none of the eight stale ones
