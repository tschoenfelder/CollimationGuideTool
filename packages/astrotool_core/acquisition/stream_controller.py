"""StreamController — background-thread capture + single-slot mailbox on
top of CameraPort.capture().

Ported from smart_telescope's ``services.managed_camera`` (``ManagedCamera``,
``FrameMailbox``), renamed and generalized: the "role" concept (main/guide/
oag, tied to smart_telescope's multi-camera setup) is dropped in favor of a
plain thread-naming ``name`` — neither app needs more than one camera role.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

from astrotool_core.acquisition.acquisition_state import AcquisitionState
from astrotool_core.camera.port import CameraPort, CaptureAbortedError
from astrotool_core.frames.frame import Frame

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MailboxFrame:
    """A single captured frame as stored in the mailbox."""

    sequence: int
    captured_at_monotonic: float
    frame: Frame
    dropped_before: int = 0


#: How many recent frames the mailbox remembers for non-destructive readers.
_RING_LENGTH = 8


class FrameMailbox:
    """Single-slot latest-frame mailbox.

    Callers that produce frames faster than the consumer can read them see
    intermediate frames silently dropped. ``dropped_count`` counts total drops.

    Issue #49: ``wait_latest`` is DESTRUCTIVE (it pops the pending frame), so it
    has exactly one consumer -- the live view. Anything else that needs fresh
    frames (a calibration capture waiting for a stable window, which can take
    tens of seconds with long exposures) reads the small ring of recent frames
    through ``wait_next_after`` instead: non-destructive and ordered, safe on a
    worker thread, and it can never starve the live view (or vice versa).
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._pending: MailboxFrame | None = None
        self._dropped = 0
        self._ring: deque[MailboxFrame] = deque(maxlen=_RING_LENGTH)

    def put(self, frame: Frame, *, sequence: int, captured_at: float) -> None:
        with self._cond:
            if self._pending is not None:
                self._dropped += 1
            self._pending = MailboxFrame(sequence, captured_at, frame, self._dropped)
            self._ring.append(self._pending)
            self._cond.notify_all()

    def wait_latest(
        self, *, after_sequence: int = 0, timeout_s: float = 0.2
    ) -> MailboxFrame | None:
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                if self._pending is not None and self._pending.sequence > after_sequence:
                    frame = self._pending
                    self._pending = None
                    return frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)

    def wait_next_after(self, sequence: int, *, timeout_s: float) -> MailboxFrame | None:
        """The OLDEST remembered frame newer than `sequence`, without consuming it
        (see the class docstring); blocks up to `timeout_s` for one to arrive.
        Frames come back in order, so a caller walking `sequence` forward sees
        every frame the ring still holds, exactly once."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while True:
                for candidate in self._ring:
                    if candidate.sequence > sequence:
                        return candidate
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)

    @property
    def dropped_count(self) -> int:
        with self._cond:
            return self._dropped


class StreamController:
    """Wraps a CameraPort with a background capture thread and latest-frame mailbox."""

    def __init__(self, camera: CameraPort, *, name: str = "camera") -> None:
        self.camera = camera
        self.name = name
        self.mailbox = FrameMailbox()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._seq = 0
        self._error: Exception | None = None
        self._err_lock = threading.Lock()

    @property
    def state(self) -> AcquisitionState:
        with self._err_lock:
            has_error = self._error is not None
        if has_error:
            return AcquisitionState.ERROR
        if self._thread is not None and self._thread.is_alive():
            return AcquisitionState.STREAMING
        return AcquisitionState.IDLE

    def start_stream(self, exposure_s: float, cadence_s: float) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # already running; no-op
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(exposure_s, cadence_s),
            daemon=True,
            name=f"stream-{self.name}",
        )
        self._thread.start()

    def stop_stream(self) -> None:
        self._stop_event.set()
        self.camera.abort_capture()
        if self._thread is not None:
            self._thread.join(timeout=10.0)
            if self._thread.is_alive():
                _log.warning("stream-%s thread did not exit within 10s", self.name)
            self._thread = None

    def pop_stream_error(self) -> Exception | None:
        with self._err_lock:
            err, self._error = self._error, None
            return err

    def _run(self, exposure_s: float, cadence_s: float) -> None:
        while not self._stop_event.is_set():
            try:
                cycle_start = time.monotonic()
                frame = self.camera.capture(exposure_s)
                captured_at = time.monotonic()  # after capture, to reflect true frame age
                self._seq += 1
                self.mailbox.put(frame, sequence=self._seq, captured_at=captured_at)
                elapsed = time.monotonic() - cycle_start
                sleep_s = max(0.0, cadence_s - elapsed)
                if sleep_s > 0:
                    self._stop_event.wait(timeout=sleep_s)
            except CaptureAbortedError:
                break
            except Exception as exc:
                with self._err_lock:
                    self._error = exc
                break
