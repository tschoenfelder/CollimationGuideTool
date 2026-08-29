"""GuideTool entry point — console script `guide-tool`.

Wires the default dev config: `FakeCamera` (a single bright star, no
mount configured — this app doesn't need a donut shape the way
CollimationTool does, so PLAN.md's literal "fake_camera by default"
applies directly here). Swapping in a real camera/mount later is a
one-line change in `_default_camera()`; no other file needs to know.
"""

from __future__ import annotations

import logging
import sys
from types import TracebackType

from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.port import CameraPort
from astrotool_core.diagnostics import DiagnosticService, RecentLogHandler
from PySide6.QtWidgets import QApplication

from guide_tool.ui.main_window import MainWindow

_log = logging.getLogger(__name__)


def _default_camera() -> CameraPort:
    return FakeCamera()


def _install_excepthook(diagnostics: DiagnosticService) -> None:
    """Automatic-capture boundary for issue #10 — see CollimationTool's
    main.py for the full rationale; identical mechanism, shared here only
    by convention (astrotool_core.diagnostics has no app-specific state)."""
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
            window = QApplication.activeWindow()
            status_label = getattr(window, "_diagnostics_status_label", None)
            if status_label is not None:
                # Raw UUID only — the field is a read-only, selectable/copyable
                # QLineEdit (issue #11); the log line above carries the framing.
                status_label.setText(bundle.incident_id)
        previous_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook


def main() -> None:
    app = QApplication(sys.argv)

    log_handler = RecentLogHandler()
    logging.getLogger().addHandler(log_handler)
    diagnostics = DiagnosticService(app_name="GuideTool", recent_logs=log_handler.lines)
    _install_excepthook(diagnostics)

    window = MainWindow(_default_camera(), diagnostics=diagnostics)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
