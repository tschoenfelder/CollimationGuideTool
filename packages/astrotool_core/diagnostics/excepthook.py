"""The unhandled-exception diagnostic boundary (issue #10), shared by both apps (#55 K04, S6.9).

Wraps whatever excepthook is already installed (PySide6 routes exceptions raised inside Qt
slots through sys.excepthook) so the original traceback is still printed exactly as before --
diagnostic capture only adds a bundle, it never hides or replaces the failure. Toolkit-free:
the app passes its own "active window" lookup (`QApplication.activeWindow`).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from types import TracebackType

from astrotool_core.diagnostics.service import DiagnosticService

_log = logging.getLogger(__name__)


def install_excepthook(
    diagnostics: DiagnosticService, *, active_window: Callable[[], object]
) -> None:
    """Install the capture hook. A captured incident's id is shown in the active window's
    `_diagnostics_status_label`, if it has one."""
    previous_hook = sys.excepthook

    def _hook(
        exc_type: type[BaseException], exc_value: BaseException, exc_tb: TracebackType | None
    ) -> None:
        bundle = diagnostics.capture_exception(exc_value)
        if bundle is not None:
            _log.error(
                "Unhandled exception captured as diagnostic incident %s at %s",
                bundle.incident_id,
                bundle.path,
            )
            status_label = getattr(active_window(), "_diagnostics_status_label", None)
            if status_label is not None:
                # Raw UUID only -- the field is a read-only, selectable/copyable
                # QLineEdit (issue #11); the log line above carries the framing.
                status_label.setText(bundle.incident_id)
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
