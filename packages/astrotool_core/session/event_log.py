"""EventLogger — structured per-call logging on top of SessionContext.

Ported from smart_telescope's ``ServiceCallLogger``/``ServiceCallRecord``
(services/service_call_logger.py, domain/service_call_log.py), renamed to
drop the smart_telescope-specific "service call" framing — this module has
no opinion on what kind of event it's logging.

Usage::

    with event_logger.event("collimation", "CollimationAdvisor") as ev:
        result = advise(frame)
        ev.set_response({"status": "ok", "recommendation": result.summary})
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from astrotool_core.session.session_context import SessionContext

_log = logging.getLogger(__name__)


@dataclass
class EventRecord:
    """One structured log entry for a single logged operation."""

    session_id: str
    event_name: str
    run_id: str
    iteration: int
    timestamp: str  # ISO-8601 UTC string
    input_frame_filename: str | None
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None
    duration_ms: float
    status: str  # "ok" | "failed" | "cancelled"
    error_if_any: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "event_name": self.event_name,
            "run_id": self.run_id,
            "iteration": self.iteration,
            "timestamp": self.timestamp,
            "input_frame_filename": self.input_frame_filename,
            "request_payload": self.request_payload,
            "response_payload": self.response_payload,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_if_any": self.error_if_any,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), default=str)


class _EventContext:
    """Context manager for one event log record."""

    def __init__(
        self,
        session: SessionContext,
        section: str,
        event_name: str,
        run_id: str,
        iteration: int,
        request_payload: dict[str, Any],
        input_frame_filename: str | None,
    ) -> None:
        self._session = session
        self._section = section
        self._event_name = event_name
        self._run_id = run_id
        self._iteration = iteration
        self._request_payload = request_payload
        self._input_frame = input_frame_filename
        self._start_ms = time.monotonic() * 1000
        self._timestamp = datetime.now(UTC).isoformat()
        self._response: dict[str, Any] | None = None
        self._explicit_error: str | None = None
        self._cancelled = False

    def set_response(self, payload: dict[str, Any]) -> None:
        """Attach a response payload to emit on exit."""
        self._response = payload

    def set_error(self, error: str) -> None:
        """Mark the event as failed (for caught exceptions that return early)."""
        self._explicit_error = error

    def mark_cancelled(self) -> None:
        """Mark the event as cancelled."""
        self._cancelled = True

    def __enter__(self) -> _EventContext:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        duration_ms = time.monotonic() * 1000 - self._start_ms

        if self._explicit_error is not None:
            status = "failed"
            error = self._explicit_error
        elif self._cancelled:
            status = "cancelled"
            error = None
        elif exc_val is not None:
            status = "failed"
            error = f"{type(exc_val).__name__}: {exc_val}"
        else:
            status = "ok"
            error = None

        record = EventRecord(
            session_id=self._session.session_slug,
            event_name=self._event_name,
            run_id=self._run_id,
            iteration=self._iteration,
            timestamp=self._timestamp,
            input_frame_filename=self._input_frame,
            request_payload=self._request_payload,
            response_payload=self._response,
            duration_ms=round(duration_ms, 1),
            status=status,
            error_if_any=error,
        )
        try:
            self._session.get_logger(self._section).info("%s", record.to_json_line())
        except Exception as exc:
            _log.warning("EventLogger: failed to write record: %s", exc)


class EventLogger:
    """Structured per-call logging wired to a SessionContext's section loggers."""

    def __init__(self, session: SessionContext) -> None:
        self._session = session

    def event(
        self,
        section: str,
        event_name: str,
        request_payload: dict[str, Any] | None = None,
        input_frame_filename: str | None = None,
        run_id: str | None = None,
        iteration: int = 0,
    ) -> _EventContext:
        """Open an event log context manager.

        Args:
            section: section name (logger namespace this event is written to).
            event_name: human-readable operation name.
            request_payload: dict of inputs (no image data — use a filename).
            input_frame_filename: path to an input frame, if applicable.
            run_id: caller-supplied run ID; generated if None.
            iteration: 0-based iteration index within one run.
        """
        return _EventContext(
            session=self._session,
            section=section,
            event_name=event_name,
            run_id=run_id or str(uuid.uuid4())[:8],
            iteration=iteration,
            request_payload=request_payload or {},
            input_frame_filename=input_frame_filename,
        )
