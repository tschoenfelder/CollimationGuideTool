"""CollimationTool main window — two side-by-side camera panels plus a
shared diagnostics action.

New (Stage 7): smart_telescope's UI is a browser/JS frontend against a
FastAPI backend, so there is no PySide6 analog to port.

Two-camera layout: the left panel is the primary/collimation camera, the
right is a guide camera to watch in parallel — each is a full,
independent `CameraPanel` (its own connection, streaming, exposure/gain,
auto-exposure, and collimation measurement; see that module's docstring).
The two panels' camera pickers are cross-wired so connecting a real
device on one side removes it from the other's combo — a ToupTek camera
only allows one open handle at a time, so this isn't just a UX nicety.
The two live views are independently sized widgets, each preserving its
own aspect ratio (see `LiveViewLabel`) rather than sharing one pixel
scale — there's no requirement that the two cameras even share a native
resolution.

Deliberately not wired for Stage 7: `CollimationRecenterPolicy` (SCT
collimation screws are turned by hand; recentering the whole scope via
the mount is a separate, not-yet-decided operator workflow) and the
Tri-Bahtinov fine-collimation pathway (deferred since Stage 5 — see
docs/porting-notes.md).

Diagnostics (issue #10): one "Capture diagnostics" action for the whole
window (not duplicated per panel — capturing evidence is an app-level
concept, not a per-camera one) writes a UUID-identified bundle via the
shared `DiagnosticService` (`diagnostics` constructor param, injectable
for testing) — same bundle format the app's unhandled-exception boundary
uses (see main.py). The context/frame providers aggregate both panels'
state under "left"/"right" keys.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astrotool_core.acquisition.auto_exposure import AutoExposureConfig
from astrotool_core.camera import CameraPort, FakeCamera, TouptekDeviceInfo
from astrotool_core.camera import (
    list_devices as _list_touptek_devices,
)
from astrotool_core.diagnostics import DiagnosticService
from astrotool_core.frames.frame import Frame
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.ui.camera_panel import CameraPanel, default_camera_factory

_DEFAULT_MANUAL_REASON = "Manual capture from UI (no note given)"


class MainWindow(QMainWindow):
    def __init__(
        self,
        camera: CameraPort,
        *,
        guide_camera: CameraPort | None = None,
        device_lister: Callable[[], list[TouptekDeviceInfo]] = _list_touptek_devices,
        camera_factory: Callable[[str], CameraPort] = default_camera_factory,
        diagnostics: DiagnosticService | None = None,
        auto_exposure_config: AutoExposureConfig | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("CollimationTool")

        self._left_panel = CameraPanel(
            camera,
            title="Main",
            device_lister=device_lister,
            camera_factory=camera_factory,
            auto_exposure_config=auto_exposure_config,
        )
        self._right_panel = CameraPanel(
            guide_camera if guide_camera is not None else FakeCamera(),
            title="Guide",
            device_lister=device_lister,
            camera_factory=camera_factory,
            auto_exposure_config=auto_exposure_config,
        )
        self._left_panel.connected_device_changed.connect(self._on_left_camera_changed)
        self._right_panel.connected_device_changed.connect(self._on_right_camera_changed)

        self._diagnostics = diagnostics or DiagnosticService(app_name="CollimationTool")
        self._diagnostics.set_context_provider(self._diagnostic_context)
        self._diagnostics.set_frame_provider(self._all_recent_frames)

        self._diagnostics_note = QLineEdit()
        self._diagnostics_note.setPlaceholderText("What looked wrong? (optional)")
        self._capture_diagnostics_button = QPushButton("Capture diagnostics")
        self._capture_diagnostics_button.clicked.connect(self._on_capture_diagnostics)
        # A read-only QLineEdit (not a QLabel) so the incident UUID is
        # selectable/copyable via normal text-field interaction — see
        # issue #11. A "Copy" button covers the one-click case too.
        self._diagnostics_status_label = QLineEdit("")
        self._diagnostics_status_label.setReadOnly(True)
        self._diagnostics_copy_button = QPushButton("Copy")
        self._diagnostics_copy_button.clicked.connect(self._on_copy_diagnostics_status)

        diagnostics_row = QHBoxLayout()
        diagnostics_row.addWidget(self._diagnostics_note, stretch=1)
        diagnostics_row.addWidget(self._capture_diagnostics_button)
        diagnostics_row.addWidget(self._diagnostics_status_label, stretch=1)
        diagnostics_row.addWidget(self._diagnostics_copy_button)

        panels_row = QHBoxLayout()
        panels_row.addWidget(self._left_panel, stretch=1)
        panels_row.addWidget(self._right_panel, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(diagnostics_row)
        layout.addLayout(panels_row, stretch=1)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.resize(1360, 700)

    def _on_left_camera_changed(self, device: object) -> None:
        excluded = device.camera_id if isinstance(device, TouptekDeviceInfo) else None
        self._right_panel.refresh_camera_list(excluded)

    def _on_right_camera_changed(self, device: object) -> None:
        excluded = device.camera_id if isinstance(device, TouptekDeviceInfo) else None
        self._left_panel.refresh_camera_list(excluded)

    def _diagnostic_context(self) -> dict[str, Any]:
        return {
            "left": self._left_panel.diagnostic_context(),
            "right": self._right_panel.diagnostic_context(),
        }

    def _all_recent_frames(self) -> list[Frame]:
        return self._left_panel.recent_frames() + self._right_panel.recent_frames()

    def _on_capture_diagnostics(self) -> None:
        reason = self._diagnostics_note.text().strip() or _DEFAULT_MANUAL_REASON
        bundle = self._diagnostics.capture_manual(reason=reason)
        if bundle is None:
            self._diagnostics_status_label.setText("Diagnostics capture failed — see logs.")
            return
        # Just the raw UUID (not "Diagnostics captured: <uuid>" prose) — the
        # field exists so this can be selected/copied cleanly (issue #11);
        # the log line above still carries the human-readable framing.
        self._diagnostics_status_label.setText(bundle.incident_id)
        self._diagnostics_note.clear()

    def _on_copy_diagnostics_status(self) -> None:
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self._diagnostics_status_label.text())

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 — Qt override
        self._left_panel.stop()
        self._right_panel.stop()
        super().closeEvent(event)
