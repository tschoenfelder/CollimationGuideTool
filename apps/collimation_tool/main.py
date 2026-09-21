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
from astrotool_core.filter_wheel.registry import (
    FilterWheelAssignment,
    build_filter_wheels,
    load_filter_wheel_layout,
)
from astrotool_core.focus.port import FocuserPort
from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.mount.port import MountPort
from astrotool_core.onstep import (
    OnStepConnection,
    OnStepFocuserAdapter,
    OnStepMountParkAdapter,
    OnStepMountPulseAdapter,
    load_onstep_settings,
)
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


def _default_onstep_connection() -> OnStepConnection:
    """The ONE OnStep connection this app has: OnStepAdapter's `OnStepClient`
    on the configured serial port (`[onstep] serial_port` in
    ~/.CollimationGuideTool/config.toml or `ONSTEP_PORT`). The focuser, the
    park/unpark control and the Mount Align pulses all share it; nothing
    here may reach the OnStep controller any other way (AGENTS.md)."""
    settings = load_onstep_settings()
    return OnStepConnection(settings.serial_port, baud_rate=settings.baud_rate)


def _default_focuser(connection: OnStepConnection) -> FocuserPort:
    """The main optical train's OnStep focuser, via OnStepAdapter."""
    return OnStepFocuserAdapter(connection)


def _default_mount(connection: OnStepConnection) -> MountParkPort:
    """The OnStep mount's park/unpark/tracking control, via OnStepAdapter."""
    return OnStepMountParkAdapter(connection)


def _default_filter_wheels() -> list[FilterWheelAssignment]:
    """The rig's physical filter wheel(s) and which optical trains use them
    (issue #41): by default ONE wheel, `ToupTek EFW 1`, shared by Main and
    OAG, none for Guide -- overridable in `~/.CollimationGuideTool/config.toml`
    (see `astrotool_core.filter_wheel.registry`). This used to be missing
    entirely, so every filter-wheel panel silently used `NoFilterWheel`."""
    return build_filter_wheels(load_filter_wheel_layout())


def _default_pulse_mount(connection: OnStepConnection) -> MountPort:
    """The OnStep mount's bounded directional motion for the Mount Align
    panel, via OnStepAdapter's timed-move API."""
    return OnStepMountPulseAdapter(connection)


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
    # The root logger's default level (WARNING) silently drops every
    # _log.info() call in this app's/astrotool_core's own code before it
    # ever reaches log_handler above — found investigating a focuser
    # incident whose application.log showed only WARNING/ERROR lines,
    # never the INFO-level move-by-move detail that had just been added
    # for exactly this purpose. Scoped to our own namespaces (not the
    # bare root) so third-party libraries' own INFO chatter stays quiet.
    logging.getLogger("astrotool_core").setLevel(logging.INFO)
    logging.getLogger("collimation_tool").setLevel(logging.INFO)
    diagnostics = DiagnosticService(app_name="CollimationTool", recent_logs=log_handler.lines)
    _install_excepthook(diagnostics)

    # Two independent ReplayCamera instances — the guide panel's demo
    # camera can't be the same object as the main panel's (see
    # CameraPanel's docstring: each panel owns its own StreamController).
    onstep = _default_onstep_connection()
    window = MainWindow(
        _default_camera(),
        guide_camera=_default_camera(),
        focuser=_default_focuser(onstep),
        mount=_default_mount(onstep),
        pulse_mount=_default_pulse_mount(onstep),
        filter_wheels=_default_filter_wheels(),
        threaded_captures=True,  # issue #49: never block the GUI thread on a frame wait
        diagnostics=diagnostics,
    )
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
