"""CollimationTool entry point — console script `collimation-tool`.

Wires the default dev config: a synthetic donut sequence served by
`ReplayCamera` (a live `FakeCamera` star is round, not a donut, so it
can't exercise `DonutAnalyzer` — this is the one place Stage 7 departs
from PLAN.md's literal "fake_camera by default" for the sake of an
actually-working demo). Swapping in a real camera later is a one-line
change in `_default_camera()`; no other file needs to know.
"""

from __future__ import annotations

import logging
import sys
from types import TracebackType

from astrotool_core.camera.port import CameraPort
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.diagnostics import DiagnosticService, RecentLogHandler
from astrotool_core.testing.frame_factory import donut_image
from PySide6.QtWidgets import QApplication

from collimation_tool.ui.main_window import MainWindow

_log = logging.getLogger(__name__)

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


def _install_excepthook(diagnostics: DiagnosticService) -> None:
    """Automatic-capture boundary for issue #10.

    Wraps whatever excepthook is already installed (PySide6 routes
    exceptions raised inside Qt slots through sys.excepthook) so the
    original traceback is still printed exactly as before — diagnostic
    capture only adds a bundle, it never hides or replaces the failure.
    Enriched with whatever the active MainWindow last registered via
    `set_context_provider`/`set_frame_provider` (see its docstring).
    """
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
    diagnostics = DiagnosticService(app_name="CollimationTool", recent_logs=log_handler.lines)
    _install_excepthook(diagnostics)

    window = MainWindow(_default_camera(), diagnostics=diagnostics)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
