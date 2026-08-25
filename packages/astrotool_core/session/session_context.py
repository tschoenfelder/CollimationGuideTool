"""SessionContext — per-session identity plus lazily-created per-section loggers.

Ported from smart_telescope's ``services.section_logger.SectionLogger``,
generalized: smart_telescope hardcodes a fixed ``LOG_SECTIONS`` tuple of
app-specific names ("goto", "click_to_center", ...). astrotool_core has no
opinion on what sections either app uses, so sections are created lazily
on first request instead.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

_FORMATTER = logging.Formatter(
    "%(asctime)s %(levelname)-8s [%(session_id)s] %(section)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

_log = logging.getLogger(__name__)


class _SectionAdapter(logging.LoggerAdapter[logging.Logger]):
    """Injects session_id and section into every log record."""

    # Overrides logging.LoggerAdapter's own Any-typed signature.
    def process(self, msg: Any, kwargs: MutableMapping[str, Any]) -> tuple[  # noqa: ANN401
        Any, MutableMapping[str, Any]
    ]:
        extra = kwargs.setdefault("extra", {})
        extra.update(self.extra or {})
        return msg, kwargs


class SessionContext:
    """One session_id shared across a running app instance, plus per-section loggers.

    Pass ``log_dir`` to also write each section to
    ``{log_dir}/{session_slug}/{section}.log``; omit it for in-memory-only
    logging (the default — useful in tests).
    """

    def __init__(self, *, log_dir: str | None = None, session_id: str | None = None) -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self._log_dir = log_dir
        self._lock = threading.Lock()
        self._adapters: dict[str, _SectionAdapter] = {}
        self._paths: dict[str, str] = {}

    @property
    def session_slug(self) -> str:
        return self.session_id[:8]

    def get_logger(self, section: str) -> _SectionAdapter:
        with self._lock:
            adapter = self._adapters.get(section)
            if adapter is not None:
                return adapter

            logger = logging.getLogger(f"astrotool_core.section.{section}")
            logger.setLevel(logging.DEBUG)
            logger.propagate = True

            if self._log_dir:
                path = Path(self._log_dir) / self.session_slug / f"{section}.log"
                try:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    handler = logging.FileHandler(str(path), encoding="utf-8")
                    handler.setFormatter(_FORMATTER)
                    logger.addHandler(handler)
                    self._paths[section] = str(path)
                except OSError as exc:
                    _log.warning("SessionContext: cannot open log file %s: %s", path, exc)

            adapter = _SectionAdapter(
                logger,
                {"session_id": self.session_slug, "section": section},
            )
            self._adapters[section] = adapter
            return adapter

    def get_paths(self) -> dict[str, str]:
        """Return {section: absolute_file_path} for every section opened so far."""
        return dict(self._paths)

    def close(self) -> None:
        """Close all file handlers."""
        with self._lock:
            for adapter in self._adapters.values():
                logger = adapter.logger
                for handler in list(logger.handlers):
                    with contextlib.suppress(Exception):
                        handler.close()
                    logger.removeHandler(handler)
