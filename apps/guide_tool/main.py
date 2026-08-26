"""GuideTool entry point — console script `guide-tool`.

Wires the default dev config: `FakeCamera` (a single bright star, no
mount configured — this app doesn't need a donut shape the way
CollimationTool does, so PLAN.md's literal "fake_camera by default"
applies directly here). Swapping in a real camera/mount later is a
one-line change in `_default_camera()`; no other file needs to know.
"""

from __future__ import annotations

import sys

from astrotool_core.camera.fake_camera import FakeCamera
from astrotool_core.camera.port import CameraPort
from PySide6.QtWidgets import QApplication

from guide_tool.ui.main_window import MainWindow


def _default_camera() -> CameraPort:
    return FakeCamera()


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow(_default_camera())
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
