"""Lightweight local diagnostic-bundle capture for field failures.

See GitHub issue #10. Deliberately not a database or telemetry service —
just UUID-named directories under ``~/.CollimationGuideTool/diagnostics/``
containing structured metadata, a bounded recent-log tail, and any
frames available at the time.
"""

from astrotool_core.diagnostics.recent_log_handler import RecentLogHandler
from astrotool_core.diagnostics.service import (
    DEFAULT_DIAGNOSTICS_DIR,
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MAX_BUNDLES,
    DiagnosticBundle,
    DiagnosticService,
    find_bundle,
)

__all__ = [
    "DEFAULT_DIAGNOSTICS_DIR",
    "DEFAULT_MAX_AGE_DAYS",
    "DEFAULT_MAX_BUNDLES",
    "DiagnosticBundle",
    "DiagnosticService",
    "RecentLogHandler",
    "find_bundle",
]
