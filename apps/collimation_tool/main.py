"""CollimationTool entry point — console script `collimation-tool`.

Wires the default dev config: a synthetic donut sequence served by
`ReplayCamera` (a live `FakeCamera` star is round, not a donut, so it
can't exercise `DonutAnalyzer` — this is the one place Stage 7 departs
from PLAN.md's literal "fake_camera by default" for the sake of an
actually-working demo). Swapping in a real camera later is a one-line
change in `_default_camera()`; no other file needs to know.
"""

from __future__ import annotations

import sys

from astrotool_core.camera.port import CameraPort
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.testing.frame_factory import donut_image
from PySide6.QtWidgets import QApplication

from collimation_tool.ui.main_window import MainWindow

# Same shape/radii/peak as datasets/acceptance/collimation_cases.json's
# working frame config — DonutAnalyzer's background estimation needs the
# signal (ring) area small relative to the frame, or its sigma-clipping
# never converges and every frame reads as "no_signal".
_SHAPE = (240, 240)
_CENTER = (120.0, 120.0)
_OUTER_RADIUS = 50.0
_INNER_RADIUS = 20.0


def _default_camera() -> CameraPort:
    """A small cycling donut sequence with a wandering inner-hole offset."""
    offsets = [(0.0, 0.0), (5.0, -2.0), (-8.0, 6.0), (3.0, 4.0)]
    arrays = [
        donut_image(
            _SHAPE,
            outer_center=_CENTER,
            outer_radius=_OUTER_RADIUS,
            inner_center=(_CENTER[0] + dx, _CENTER[1] + dy),
            inner_radius=_INNER_RADIUS,
            peak=3000.0,
            background=100.0,
        )
        for dx, dy in offsets
    ]
    return ReplayCamera.from_arrays(arrays, cycle=True)


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow(_default_camera())
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
