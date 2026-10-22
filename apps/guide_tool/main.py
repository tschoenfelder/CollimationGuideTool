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

from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.port import CameraPort
from astrotool_core.diagnostics import DiagnosticService, RecentLogHandler
from astrotool_core.diagnostics.excepthook import install_excepthook
from PySide6.QtWidgets import QApplication

from guide_tool.ui.main_window import MainWindow

_log = logging.getLogger(__name__)


def _default_camera() -> CameraPort:
    return FakeCamera()


def _install_excepthook(diagnostics: DiagnosticService) -> None:
    """Automatic-capture boundary for issue #10 -- one shared helper (#55 K04):
    `astrotool_core.diagnostics.excepthook.install_excepthook`."""
    install_excepthook(diagnostics, active_window=QApplication.activeWindow)


def main() -> None:
    app = QApplication(sys.argv)

    log_handler = RecentLogHandler()
    logging.getLogger().addHandler(log_handler)
    # See CollimationTool's main.py for why this is needed: the root
    # logger's default level (WARNING) otherwise silently drops every
    # _log.info() call in this app's/astrotool_core's own code (e.g.
    # guide_controller.py's) before it ever reaches log_handler above.
    logging.getLogger("astrotool_core").setLevel(logging.INFO)
    logging.getLogger("guide_tool").setLevel(logging.INFO)
    diagnostics = DiagnosticService(app_name="GuideTool", recent_logs=log_handler.lines)
    _install_excepthook(diagnostics)

    window = MainWindow(_default_camera(), diagnostics=diagnostics)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
