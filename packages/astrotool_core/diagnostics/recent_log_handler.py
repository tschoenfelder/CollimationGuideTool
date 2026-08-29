"""RecentLogHandler — bounded in-memory tail of formatted log lines.

Supports GitHub issue #10's "recent application logs, preferably a
bounded window rather than unlimited history" requirement: attach one
instance to the root logger for the lifetime of the app, then pull its
``lines()`` into a diagnostic bundle on capture. Memory use is bounded by
``capacity`` regardless of how long the app has been running.
"""

from __future__ import annotations

import logging
from collections import deque

_DEFAULT_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


class RecentLogHandler(logging.Handler):
    """A ``logging.Handler`` that keeps only the last *capacity* lines."""

    def __init__(self, capacity: int = 500, level: int = logging.NOTSET) -> None:
        super().__init__(level=level)
        self.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        self._lines: deque[str] = deque(maxlen=capacity)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._lines.append(self.format(record))
        except Exception:
            self.handleError(record)

    def lines(self) -> list[str]:
        """Return the currently buffered lines, oldest first."""
        return list(self._lines)
