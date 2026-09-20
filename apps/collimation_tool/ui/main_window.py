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

Deliberately not wired for docs/porting-notes.md's own "Stage 7" (UI
port milestone, a different numbering scheme from the fine-collimation
requirements doc's 12-stage pipeline below): `CollimationRecenterPolicy`
(SCT collimation screws are turned by hand; recentering the whole scope
via the mount is a separate, not-yet-decided operator workflow) and the
Tri-Bahtinov fine-collimation pathway (deferred since Stage 5 — see
docs/porting-notes.md).

Fine collimation (issue #21, `resources/requirements/2026-08-31-fine-
collimation-requirements.md`'s own Stage 7 — the maskless pipeline, NOT
the Tri-Bahtinov pathway above): `FineCollimationPanel` wires Stages
1-6 (#15-#20) together live against `_left_panel` (Main) only, same
Main-train-only pairing as the focuser. `optical_config` (injectable,
defaulting to a fully-unconfigured `OpticalConfig()` — no telescope
parameters are sourced from config.toml yet, a separate later concern
matching #19's own deferred scope) only ever *softens* the Stage 6
symmetry measurement when absent (see #20's own soft-dependency design)
— never blocks it.

Diagnostics (issue #10): one "Capture diagnostics" action for the whole
window (not duplicated per panel — capturing evidence is an app-level
concept, not a per-camera one) writes a UUID-identified bundle via the
shared `DiagnosticService` (`diagnostics` constructor param, injectable
for testing) — same bundle format the app's unhandled-exception boundary
uses (see main.py). The context/frame providers aggregate both panels'
state under "left"/"right" keys.

Guide-frame FOV overlay: the right (guide) panel's live view draws a
yellow rectangle showing where the left (main) camera's field of view
falls within it — see `collimation_tool.ui.fov_overlay`. The optical
trains' plate scale is the *master* config for this and is read exactly
once, here at startup (`main_pixel_scale_arcsec`/`guide_pixel_scale_arcsec`,
each defaulting to `astrotool_core.optics.load_pixel_scale_arcsec()`
against SmartTScope's config.toml if not given) — never re-read per
frame or per poll; a config change requires restarting the app, same as
any other startup-read config. Recomputed only when either panel's
connected camera changes (its sensor resolution is the other input).

Focuser: `FocuserPanel` (`focuser` constructor param, defaulting to
`NoFocuser` — a `FocuserPort`, same injectable-default pattern as
`camera`/`guide_camera`) gives manual in/out jog control (1/10/100/200 steps)
over the main optical train's OnStep focuser, connected via a real
indiserver — see `astrotool_core.focus.indi_focuser_adapter`'s docstring
for why this one, unlike `mount/indi_adapter.py`, genuinely speaks INDI.
Since the focuser sits in the main train only, `_left_panel` (Main) pauses
its own poll loop for the duration of every jog (`FocuserPanel.
move_in_flight_changed` -> `CameraPanel.set_updates_paused`) so live
analysis/display never runs on a frame captured mid-move; `_right_panel`
(Guide) is unaffected.

Mount: `MountParkPanel` (`mount` constructor param, defaulting to
`NoMountPark`) gives park/unpark-only control over the OnStep mount, over
the same real indiserver connection as the focuser (a separate
`IndiClient` socket to the same device) — see
`astrotool_core.mount.indi_mount_park_adapter` and `MountParkPort`'s own
docstring for why this is a deliberately separate, narrower port than
`MountPort` (which is guiding-pulse-only). Unparking always immediately
deactivates tracking too, rather than trusting the mount's own
post-unpark default.

Filter wheels (issue #34): two `FilterWheelPanel`s (`main_filter_wheel`/
`guide_filter_wheel` constructor params, each defaulting to
`NoFilterWheel` — a `FilterWheelPort`, same injectable-default pattern
as `camera`/`guide_camera`, not the focuser's Main-only pairing, since
the issue explicitly requires "must not assume exactly one filter wheel
globally"). Read-only status display only (current slot, configured
filter name, moving state, an explicit unavailable/unknown reason) —
no commanding, see `astrotool_core.filter_wheel.port`'s own docstring.

Mount alignment: `MountTestMovePanel` (see its own docstring) is wired to
both `CameraPanel`s' `set_auto_exposure_paused` (not `set_updates_paused` --
it still needs fresh frames captured during a calibration/nudge, just with
exposure/gain held stable across a step's before/after pair) — real incident
ca728d27, where live auto-exposure roughly doubled a camera's gain between
those two captures and corrupted the measured displacement.

Startup settings restore: each panel's connected camera (by camera_id)
plus exposure/gain/auto-exposure is saved to
`~/.CollimationGuideTool/config.toml` (`camera_settings_path`, injectable
for testing) on every change and restored on the next launch — see
`astrotool_core.config.camera_settings`'s docstring for the "hardware
will not change each time" reasoning behind trusting a saved camera_id.
A saved camera no longer enumerated is a graceful fall-back to the demo
camera, not an error.

That config-only rectangle is always centered and unrotated — it has no
way to reflect how the two cameras are actually mounted relative to each
other. "Calibrate FOV" replaces it with a real, possibly-rotated match:
`astrotool_core.registration.terrestrial_registrar.TerrestrialRegistrar`
(issue #29's cross-camera registration architecture — see that module's
own docstring) content-matches the two panels' latest captured frames,
using each optical train's own `OpticalPrior` (config-derived plate
scale + this panel's own sensor dimensions) only as a starting-point
scale estimate, and, on a confident match, sets the guide panel's polygon
overlay via `set_fov_polygon` — see `FovCalibrator` for why this runs on
a background thread rather than inline on the button click. An explicit,
user-triggered action by
default, not something re-run automatically: the two scopes' relative
mounting doesn't drift frame to frame, only when the rig is physically
adjusted. The "Keep calibrating" checkbox opts into repeating it anyway
— typically while actively adjusting that physical alignment and
wanting to see each attempt's result without a fresh click every time —
by restarting a new run as soon as each one finishes and the overlay
updates (see _poll_fov_calibration); unchecking it takes effect after
the run in flight finishes, not mid-search.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from astrotool_core.acquisition.auto_exposure import AutoExposureConfig
from astrotool_core.camera import CameraPort, FakeCamera, TouptekDeviceInfo
from astrotool_core.camera import (
    list_devices as _list_touptek_devices,
)
from astrotool_core.config import (
    DEFAULT_CONFIG_PATH,
    load_camera_settings,
    load_mount_alignment_settings,
    save_camera_settings,
)
from astrotool_core.diagnostics import DiagnosticService
from astrotool_core.diffraction.optical_reference_model import OpticalConfig
from astrotool_core.filter_wheel.no_filter_wheel import NoFilterWheel
from astrotool_core.filter_wheel.port import FilterWheelPort
from astrotool_core.focus.no_focuser import NoFocuser
from astrotool_core.focus.port import FocuserPort
from astrotool_core.frames.frame import Frame
from astrotool_core.mount.no_mount import NoMountAdapter
from astrotool_core.mount.no_mount_park import NoMountPark
from astrotool_core.mount.park_port import MountParkPort
from astrotool_core.mount.port import MountPort
from astrotool_core.optics import load_pixel_scale_arcsec
from astrotool_core.registration.alignment import derive_alignment_guidance
from astrotool_core.registration.astap_adapter import AstapCliSolver, AstapSolver
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.registration.result import CrossCameraRegistrationResult, RegistrationMethod
from PySide6.QtCore import QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from collimation_tool.application.autofocus_controller import ExposureControl
from collimation_tool.application.star_acquisition import (
    AcquisitionResult,
    AcquisitionStatus,
    FocusedStarAcquisition,
)
from collimation_tool.domain.target_mode import CollimationTargetMode
from collimation_tool.ui.camera_panel import CameraPanel, default_camera_factory
from collimation_tool.ui.filter_wheel_panel import FilterWheelPanel
from collimation_tool.ui.fine_collimation_panel import FineCollimationPanel
from collimation_tool.ui.focuser_panel import FocuserPanel
from collimation_tool.ui.fov_calibrator import FovCalibrator
from collimation_tool.ui.fov_overlay import compute_fov_overlay_rect
from collimation_tool.ui.mount_park_panel import MountParkPanel
from collimation_tool.ui.mount_test_move_panel import MountTestMovePanel

_CALIBRATION_POLL_INTERVAL_MS = 200

_DEFAULT_MANUAL_REASON = "Manual capture from UI (no note given)"


class MainWindow(QMainWindow):
    def __init__(
        self,
        camera: CameraPort,
        *,
        guide_camera: CameraPort | None = None,
        focuser: FocuserPort | None = None,
        main_filter_wheel: FilterWheelPort | None = None,
        guide_filter_wheel: FilterWheelPort | None = None,
        mount: MountParkPort | None = None,
        pulse_mount: MountPort | None = None,
        device_lister: Callable[[], list[TouptekDeviceInfo]] = _list_touptek_devices,
        camera_factory: Callable[[str], CameraPort] = default_camera_factory,
        diagnostics: DiagnosticService | None = None,
        auto_exposure_config: AutoExposureConfig | None = None,
        main_pixel_scale_arcsec: float | None = None,
        guide_pixel_scale_arcsec: float | None = None,
        camera_settings_path: Path | str | None = None,
        astap_solver: AstapSolver | None = None,
        optical_config: OpticalConfig | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle("CollimationTool")

        # Injectable-default pattern, same as camera/mount/focuser above --
        # real AstapCliSolver() by default (gracefully reports
        # ASTAP_UNAVAILABLE via is_available() if astap_cli isn't on
        # PATH, no crash), a fake solver in tests. See
        # FovCalibrator.submit_star_field.
        self._astap_solver = astap_solver if astap_solver is not None else AstapCliSolver()

        # Master config, read once at startup — see module docstring's
        # "Guide-frame FOV overlay". None means "no overlay data available"
        # (e.g. no SmartTScope config.toml on this machine), not an error.
        self._main_pixel_scale_arcsec = (
            main_pixel_scale_arcsec
            if main_pixel_scale_arcsec is not None
            else load_pixel_scale_arcsec("main")
        )
        self._guide_pixel_scale_arcsec = (
            guide_pixel_scale_arcsec
            if guide_pixel_scale_arcsec is not None
            else load_pixel_scale_arcsec("guide")
        )

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

        # Issue #33: Auto Focus needs camera access -- wired to the Main
        # panel only, same "focuser lives on the main optical train only"
        # pairing as move_in_flight_changed below.
        self._focuser_panel = FocuserPanel(
            focuser if focuser is not None else NoFocuser(),
            title="Main Focuser",
            get_frame=self._left_panel.latest_mono_frame,
            wait_for_frame=self._left_panel.wait_for_frame_after,
            set_auto_exposure_paused=self._left_panel.set_auto_exposure_paused,
            optical_train_label="Main",
            # Issue #33 (artificial star): may lower Main's exposure/gain while
            # autofocusing a saturating star; always restored after the run.
            exposure_control=ExposureControl(
                get=self._left_panel.current_exposure_gain,
                set=self._left_panel.apply_exposure_gain,
            ),
        )
        # The focuser lives on the main optical train only (see
        # FocuserPanel's own docstring) -- pause just the Main camera's
        # analysis/display while it's moving, not the Guide panel.
        self._focuser_panel.move_in_flight_changed.connect(self._left_panel.set_updates_paused)

        # Issue #21: wired to the Main optical train only, same pairing
        # as the focuser above -- fine collimation analyzes the same
        # focused star the Main camera tracks. optical_config defaults
        # to a fully-unconfigured OpticalConfig() (see module docstring).
        # Held as an attribute (not inlined below) since fine collimation's
        # guide-assisted reacquisition (issue #39) pulses the SAME mount
        # connection the test-move panel drives.
        self._pulse_mount: MountPort = (
            pulse_mount if pulse_mount is not None else NoMountAdapter()
        )
        self._fine_collimation_panel = FineCollimationPanel(
            get_frame=self._left_panel.latest_mono_frame,
            optical_config=optical_config,
            guide_reacquirer=self._reacquire_via_guide,
        )
        self._fine_collimation_panel.target_mode_changed.connect(
            self._on_target_mode_changed
        )

        # Issue #34: unlike the focuser, an EFW is genuinely per optical
        # train -- one panel each, mirroring camera/guide_camera's own
        # pairing rather than the focuser's Main-only shape.
        self._main_filter_wheel_panel = FilterWheelPanel(
            main_filter_wheel if main_filter_wheel is not None else NoFilterWheel(),
            title="Main Filter Wheel",
        )
        self._guide_filter_wheel_panel = FilterWheelPanel(
            guide_filter_wheel if guide_filter_wheel is not None else NoFilterWheel(),
            title="Guide Filter Wheel",
        )

        # Resolved from the module-level DEFAULT_CONFIG_PATH at call time
        # (not bound as this parameter's own default value) so tests can
        # monkeypatch this module's DEFAULT_CONFIG_PATH to redirect every
        # MainWindow() call at once, rather than needing camera_settings_path
        # threaded through every test's construction call — see conftest.py.
        # Resolved here (before _test_move_panel below, which also reads
        # this same file's [mount_alignment] table) rather than down by the
        # camera-settings restore, which used to be the first thing to need it.
        self._camera_settings_path = (
            Path(camera_settings_path) if camera_settings_path is not None else DEFAULT_CONFIG_PATH
        )

        # Shared with _test_move_panel below -- see MountTestMovePanel's
        # own docstring for why that panel drives this same MountParkPort
        # rather than owning a second, independently-connected copy of
        # the same park/unpark state.
        mount_park_port = mount if mount is not None else NoMountPark()
        self._mount_panel = MountParkPanel(mount_park_port)
        self._test_move_panel = MountTestMovePanel(
            self._pulse_mount,
            mount_park=mount_park_port,
            get_left_frame=self._left_panel.latest_mono_frame,
            get_right_frame=self._right_panel.latest_mono_frame,
            settings=load_mount_alignment_settings(self._camera_settings_path),
            set_left_auto_exposure_paused=self._left_panel.set_auto_exposure_paused,
            set_right_auto_exposure_paused=self._right_panel.set_auto_exposure_paused,
            get_left_exposure_gain=self._left_panel.current_exposure_gain,
            get_right_exposure_gain=self._right_panel.current_exposure_gain,
            wait_for_left_frame=self._left_panel.wait_for_frame_after,
            wait_for_right_frame=self._right_panel.wait_for_frame_after,
        )

        # Restore last session's connected camera + exposure/gain/
        # auto-exposure per panel — see module docstring's "Startup
        # settings restore" and astrotool_core.config.camera_settings.
        # Connected *after* the restore, not before: applying saved
        # settings shouldn't re-save the very state it just loaded.
        saved_settings = load_camera_settings(self._camera_settings_path)
        self._left_panel.apply_saved_settings(saved_settings.get("main"))
        self._right_panel.apply_saved_settings(saved_settings.get("guide"))
        self._left_panel.settings_changed.connect(self._save_camera_settings)
        self._right_panel.settings_changed.connect(self._save_camera_settings)

        self._update_fov_overlay()

        self._fov_calibrator = FovCalibrator()
        # Issue #29: two registration modes, same "run at most one at a
        # time" FovCalibrator underneath either way -- Terrestrial (NCC)
        # is the pre-existing, always-available mode and stays the
        # default; Star-field (ASTAP) needs a real astap_cli install
        # (gracefully reports ASTAP_UNAVAILABLE if missing, see
        # self._astap_solver above).
        self._registration_mode_group = QButtonGroup(self)
        self._registration_mode_group.setExclusive(True)
        self._terrestrial_mode_button = QPushButton("Terrestrial (NCC)")
        self._terrestrial_mode_button.setCheckable(True)
        self._terrestrial_mode_button.setChecked(True)  # default -- unchanged prior behavior
        self._registration_mode_group.addButton(self._terrestrial_mode_button)
        self._star_field_mode_button = QPushButton("Star-field (ASTAP)")
        self._star_field_mode_button.setCheckable(True)
        self._registration_mode_group.addButton(self._star_field_mode_button)
        # Issue #37: a single isolated point source (e.g. a pinhole/fiber
        # artificial star) -- no ASTAP, no rich terrestrial texture, just
        # one shared point + each optical train's own configured priors.
        self._artificial_star_mode_button = QPushButton("Artificial Star")
        self._artificial_star_mode_button.setCheckable(True)
        self._registration_mode_group.addButton(self._artificial_star_mode_button)
        mode_row = QHBoxLayout()
        mode_row.addWidget(self._terrestrial_mode_button)
        mode_row.addWidget(self._star_field_mode_button)
        mode_row.addWidget(self._artificial_star_mode_button)
        mode_row.addStretch(1)
        self._calibrate_fov_button = QPushButton("Calibrate FOV")
        self._calibrate_fov_button.clicked.connect(self._on_calibrate_fov)
        # Off by default — the base action is still the one-shot
        # calibration described above. Checking this (typically after
        # watching the first run complete, while still adjusting the
        # rig's physical alignment) restarts a new run as soon as each
        # one finishes and the overlay updates, instead of requiring a
        # fresh button click every cycle. See _poll_fov_calibration.
        self._auto_recalibrate_checkbox = QCheckBox("Keep calibrating")
        self._calibrate_fov_status_label = QLabel("")
        self._calibrate_fov_poll_timer = QTimer(self)
        self._calibrate_fov_poll_timer.setInterval(_CALIBRATION_POLL_INTERVAL_MS)
        self._calibrate_fov_poll_timer.timeout.connect(self._poll_fov_calibration)
        #: The result behind whatever polygon _right_panel is currently
        #: drawing (see _poll_fov_calibration) — set only on a confident
        #: match, never cleared by a later "no match" run, so this always
        #: explains the *currently shown* overlay for diagnostics. None
        #: until the first successful calibration.
        self._last_calibration_result: CrossCameraRegistrationResult | None = None
        #: Issue #37: the latest completed calibration attempt regardless
        #: of outcome (unlike _last_calibration_result above, which only
        #: ever holds a confident match) -- so a failed/ambiguous
        #: artificial-star attempt still leaves a diagnostic trace. Every
        #: mode gets this for free since _poll_fov_calibration is already
        #: method-agnostic.
        self._last_calibration_attempt: CrossCameraRegistrationResult | None = None
        #: The prior_b used for that same last confident result — needed
        #: to convert its guidance's px magnitude to arcsec. Same
        #: never-cleared-on-a-later-miss lifecycle as
        #: _last_calibration_result, since it describes the same overlay.
        self._last_prior_b: OpticalPrior | None = None
        #: Main's prior for that same result -- needed by guide-assisted
        #: reacquisition (issue #39) to project the Main-frame star into Guide.
        self._last_prior_a: OpticalPrior | None = None
        self._pending_prior_a: OpticalPrior | None = None
        #: The prior_b of whichever calibration run is currently
        #: in-flight -- see _last_prior_b's own docstring for why this
        #: isn't promoted to _last_prior_b until that run actually
        #: succeeds.
        self._pending_prior_b: OpticalPrior | None = None
        self._alignment_guidance_label = QLabel("")

        self._diagnostics = diagnostics or DiagnosticService(app_name="CollimationTool")
        self._diagnostics.set_context_provider(self._diagnostic_context)
        self._diagnostics.set_frame_provider(self._all_recent_frames)
        self._diagnostics.set_image_provider(self._diagnostic_images)

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

        calibration_row = QHBoxLayout()
        calibration_row.addLayout(mode_row)
        calibration_row.addWidget(self._calibrate_fov_button)
        calibration_row.addWidget(self._auto_recalibrate_checkbox)
        calibration_row.addWidget(self._calibrate_fov_status_label, stretch=1)
        calibration_row.addWidget(self._alignment_guidance_label, stretch=1)

        panels_row = QHBoxLayout()
        panels_row.addWidget(self._left_panel, stretch=1)
        panels_row.addWidget(self._right_panel, stretch=1)

        filter_wheel_row = QHBoxLayout()
        filter_wheel_row.addWidget(self._main_filter_wheel_panel, stretch=1)
        filter_wheel_row.addWidget(self._guide_filter_wheel_panel, stretch=1)

        layout = QVBoxLayout()
        layout.addLayout(diagnostics_row)
        layout.addLayout(calibration_row)
        layout.addWidget(self._focuser_panel)
        layout.addLayout(filter_wheel_row)
        layout.addWidget(self._mount_panel)
        layout.addWidget(self._test_move_panel)
        layout.addWidget(self._fine_collimation_panel)
        layout.addLayout(panels_row, stretch=1)

        central = QWidget()
        central.setLayout(layout)

        # Real incident: on a real (smaller/lower-resolution) screen, the
        # stack of control panels above panels_row (diagnostics,
        # calibration/registration mode, focuser, filter wheels, mount,
        # mount test-move, fine collimation) can exceed the available
        # screen height on its own -- with no scrolling, the camera live
        # views (panels_row, the single most important thing to see while
        # operating the telescope) were silently pushed below the visible
        # screen area with no way to reach them. A QScrollArea guarantees
        # every panel stays reachable regardless of screen size or how
        # many more panels this window grows in the future, without
        # changing the existing panel order/layout at all.
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setWidget(central)
        self.setCentralWidget(scroll_area)
        self.resize(1360, 700)

    def _on_target_mode_changed(self, mode: object) -> None:
        """Mirror the fine-collimation panel's target mode (issue #39) onto
        the rough-collimation view so both always say what they're using."""
        assert isinstance(mode, CollimationTargetMode)
        self._left_panel.set_target_mode_label(mode.label)
        # Issue #33: autofocus infers its mode from the target mode.
        self._focuser_panel.select_artificial_star_mode(
            mode is CollimationTargetMode.ARTIFICIAL_STAR
        )

    def _reacquire_via_guide(
        self, acquisition: FocusedStarAcquisition, cancel_check: Callable[[], bool] | None
    ) -> AcquisitionResult:
        """Guide-assisted recovery of a star lost from Main (issue #39),
        built from whatever #37/#29 registration and mount-test-move
        calibration currently exist. Every missing input is an EXPLICIT
        failure reason, never a silent no-op. The mount is only ever
        driven through `attempt_guide_reacquisition`'s own bounded,
        confidence-gated, cancellable `CollimationRecenterPolicy`."""
        registration = self._last_calibration_result
        prior_a = self._last_prior_a
        if registration is None or prior_a is None:
            return AcquisitionResult(AcquisitionStatus.LOST, None, None, "no_registration")
        calibration = self._test_move_panel.calibration_for("right")
        if calibration is None:
            return AcquisitionResult(
                AcquisitionStatus.LOST, None, None, "no_guide_calibration"
            )
        return acquisition.attempt_guide_reacquisition(
            self._right_panel.latest_mono_frame,
            mount=self._pulse_mount,
            guide_calibration=calibration,
            registration=registration,
            prior_main=prior_a,
            cancel_check=cancel_check,
        )

    def _on_left_camera_changed(self, device: object) -> None:
        excluded = device.camera_id if isinstance(device, TouptekDeviceInfo) else None
        self._right_panel.refresh_camera_list(excluded)
        self._update_fov_overlay()

    def _on_right_camera_changed(self, device: object) -> None:
        excluded = device.camera_id if isinstance(device, TouptekDeviceInfo) else None
        self._left_panel.refresh_camera_list(excluded)
        self._update_fov_overlay()

    def _update_fov_overlay(self) -> None:
        """Recompute the guide-frame FOV rectangle — see module docstring.
        Called whenever either panel's connected camera changes (its
        sensor resolution is the other input this needs); never re-reads
        the optical-train config itself, which was read once at startup."""
        if self._main_pixel_scale_arcsec is None or self._guide_pixel_scale_arcsec is None:
            self._right_panel.set_fov_overlay(None)
            return
        main_caps = self._left_panel.camera_descriptor().capabilities
        guide_caps = self._right_panel.camera_descriptor().capabilities
        rect = compute_fov_overlay_rect(
            main_pixel_scale_arcsec=self._main_pixel_scale_arcsec,
            main_sensor_width_px=main_caps.sensor_width_px,
            main_sensor_height_px=main_caps.sensor_height_px,
            guide_pixel_scale_arcsec=self._guide_pixel_scale_arcsec,
            guide_sensor_width_px=guide_caps.sensor_width_px,
            guide_sensor_height_px=guide_caps.sensor_height_px,
        )
        self._right_panel.set_fov_overlay(rect)
        # A previous calibration was matched against whichever camera was
        # connected before — no longer meaningful once either side's
        # camera changes (different resolution/content entirely).
        self._right_panel.set_fov_polygon(None)
        self._right_panel.set_matched_point(None)

    def _on_calibrate_fov(self) -> None:
        """Kick off a one-shot content-matching calibration — see module
        docstring's "Calibrate FOV". Runs on FovCalibrator's background
        thread; _poll_fov_calibration picks up the result."""
        main_mono = self._left_panel.latest_mono_frame()
        guide_mono = self._right_panel.latest_mono_frame()
        if main_mono is None or guide_mono is None:
            self._calibrate_fov_status_label.setText(
                "Start both streams first — no captured frame to match yet."
            )
            return
        if not self._main_pixel_scale_arcsec or not self._guide_pixel_scale_arcsec:
            # Covers both "no config found" (None) and a given-but-invalid
            # value (0.0) — either way there's nothing to divide by for a
            # starting scale estimate.
            self._calibrate_fov_status_label.setText(
                "No optical-train plate-scale config available — can't estimate a starting scale."
            )
            return
        main_caps = self._left_panel.camera_descriptor().capabilities
        guide_caps = self._right_panel.camera_descriptor().capabilities
        prior_a = OpticalPrior(
            name="main", sensor_width_px=main_caps.sensor_width_px,
            sensor_height_px=main_caps.sensor_height_px,
            pixel_scale_arcsec=self._main_pixel_scale_arcsec,
        )
        prior_b = OpticalPrior(
            name="guide", sensor_width_px=guide_caps.sensor_width_px,
            sensor_height_px=guide_caps.sensor_height_px,
            pixel_scale_arcsec=self._guide_pixel_scale_arcsec,
        )
        if self._star_field_mode_button.isChecked():
            started = self._fov_calibrator.submit_star_field(
                main_mono, guide_mono, prior_a=prior_a, prior_b=prior_b,
                solver=self._astap_solver,
            )
        elif self._artificial_star_mode_button.isChecked():
            started = self._fov_calibrator.submit_artificial_star(
                main_mono, guide_mono, prior_a=prior_a, prior_b=prior_b
            )
        else:
            started = self._fov_calibrator.submit(
                main_mono, guide_mono, prior_a=prior_a, prior_b=prior_b
            )
        if not started:
            return  # a calibration is already running
        # Held until _poll_fov_calibration knows whether this run actually
        # succeeded -- only promoted to _last_prior_b on a confident match,
        # same update point as _last_calibration_result, so the two never
        # describe two different runs.
        self._pending_prior_b = prior_b
        self._pending_prior_a = prior_a
        self._calibrate_fov_button.setEnabled(False)
        self._calibrate_fov_status_label.setText("Calibrating…")
        self._calibrate_fov_poll_timer.start()

    def _poll_fov_calibration(self) -> None:
        outcome = self._fov_calibrator.take_latest()
        if outcome is None:
            # Still running — see the real bug this progress reporting
            # was added for ("Calibration started but working without any
            # status on progress"): the search genuinely takes on the
            # order of two real minutes (see fov_registration's
            # docstring), and a static "Calibrating…" message for that
            # long is indistinguishable from a hang.
            progress = self._fov_calibrator.latest_progress()
            if progress is not None:
                completed, total = progress
                percent = (completed / total * 100.0) if total else 0.0
                self._calibrate_fov_status_label.setText(
                    f"Calibrating… {completed}/{total} ({percent:.0f}%)"
                )
            return
        self._calibrate_fov_poll_timer.stop()
        self._calibrate_fov_button.setEnabled(True)
        result = outcome.result
        self._last_calibration_attempt = result
        if not result.ok:
            self._calibrate_fov_status_label.setText(
                f"No confident {result.method.value} match found ({result.status.value}) — "
                f"keeping the previous overlay."
            )
        else:
            confidence_text = (
                f", score {result.confidence:.2f}" if result.confidence is not None else ""
            )
            self._calibrate_fov_status_label.setText(
                f"Calibrated ({result.method.value}): rotation {result.rotation_deg:.1f}°, "
                f"scale {result.scale:.4f}{confidence_text}"
            )
            assert result.polygon_a_in_b is not None  # guaranteed by .ok
            self._right_panel.set_fov_polygon(list(result.polygon_a_in_b))
            if result.method is RegistrationMethod.ARTIFICIAL_STAR:
                self._right_panel.set_matched_point(result.diagnostics.get("matched_point"))
            self._last_calibration_result = result
            if self._pending_prior_b is not None:
                self._last_prior_b = self._pending_prior_b
                self._last_prior_a = self._pending_prior_a
                guidance = derive_alignment_guidance(result, self._last_prior_b)
                if guidance is not None:
                    magnitude_arcsec = guidance.magnitude_px * self._last_prior_b.pixel_scale_arcsec
                    self._alignment_guidance_label.setText(
                        f"{guidance.description} (~{magnitude_arcsec:.0f} arcsec)"
                    )

        if self._auto_recalibrate_checkbox.isChecked():
            # Restart regardless of whether this run found a match —
            # "keep calibrating" is typically used *while* still adjusting
            # the rig's physical alignment, so a miss now is exactly the
            # case where retrying against the next frame matters most.
            # _on_calibrate_fov's own guards (no frame captured yet, no
            # plate-scale config) apply unchanged and simply won't restart
            # the poll timer if streaming has stopped in the meantime.
            self._on_calibrate_fov()

    def _diagnostic_context(self) -> dict[str, Any]:
        context: dict[str, Any] = {
            "left": self._left_panel.diagnostic_context(),
            "right": self._right_panel.diagnostic_context(),
            "focuser": self._focuser_panel.diagnostic_context(),
            "main_filter_wheel": self._main_filter_wheel_panel.diagnostic_context(),
            "guide_filter_wheel": self._guide_filter_wheel_panel.diagnostic_context(),
            "mount": self._mount_panel.diagnostic_context(),
            "mount_test_move": self._test_move_panel.diagnostic_context(),
            "fine_collimation": self._fine_collimation_panel.diagnostic_context(),
            "target_mode": self._fine_collimation_panel.target_mode.value,
        }
        stability_evidence = self._test_move_panel.diagnostic_stability_evidence()
        if stability_evidence:
            # Issue #30: per-camera evidence from the most recent
            # verify_stability=True capture(s) -- see that method's own
            # docstring. Nested separately from "mount_test_move" above
            # rather than merged into it, so a pulled bundle can tell at a
            # glance whether the stability layer even ran for this
            # attempt (real incidents like 859f2520 had no way to show
            # this before this wiring).
            context["mount_test_move_stability"] = stability_evidence
        backlash_evidence = self._test_move_panel.diagnostic_backlash_evidence()
        if backlash_evidence:
            # Issue #31 Phase C: per-camera, per-direction backlash
            # characterization from the most recent Run Calibration
            # attempt -- see diagnostic_backlash_evidence()'s own
            # docstring.
            context["mount_test_move_backlash"] = backlash_evidence
        if self._last_calibration_result is not None:
            # The calibration result behind whatever polygon the guide
            # panel is currently drawing — see _last_calibration_result's
            # docstring. Without this, a "wrong position/rotation picked"
            # report has no record of what was actually picked at all.
            context["fov_calibration"] = self._last_calibration_result
        if self._last_calibration_attempt is not None:
            # Issue #37: every completed attempt, including a failed or
            # ambiguous one -- see _last_calibration_attempt's own
            # docstring for why this is separate from the success-only
            # field above.
            context["fov_calibration_last_attempt"] = self._last_calibration_attempt
        autofocus_evidence = self._focuser_panel.diagnostic_autofocus_evidence()
        if autofocus_evidence:
            # Issue #33: the full focus curve/status/confidence from the
            # most recent Auto Focus run -- same "empty dict when nothing
            # to report" conditional-fold-in convention as the stability/
            # backlash evidence above.
            context["autofocus"] = autofocus_evidence
        return context

    def _save_camera_settings(self) -> None:
        """Persist both panels' current settings as this session's new
        startup defaults — see module docstring's "Startup settings
        restore". Wired to each panel's settings_changed signal, so this
        fires (and overwrites the whole file — see
        save_camera_settings's docstring) on every connect, exposure/gain
        edit, and auto-exposure toggle on either panel; a Pi losing power
        mid-session still keeps whatever was saved as of the last such
        change rather than only a clean shutdown's state.
        """
        save_camera_settings(
            {
                "main": self._left_panel.current_settings(),
                "guide": self._right_panel.current_settings(),
            },
            self._camera_settings_path,
        )

    def _all_recent_frames(self) -> list[Frame]:
        frames = self._left_panel.recent_frames() + self._right_panel.recent_frames()
        frames.extend(self._calibration_diagnostic_frames())
        return frames

    def _calibration_diagnostic_frames(self) -> list[Frame]:
        """The exact before/after frame pair(s) `_test_move_panel`'s last
        calibration step(s) and/or nudge actually measured against --
        see `MountTestMovePanel.diagnostic_frames()`'s own docstring
        (real incident de271da5). Tagged with a CALIBSRC header keyword
        (readable from the saved FITS file, e.g. via astropy) so a pulled
        bundle's frames/ directory identifies each one; `bit_depth=16` is
        a generic placeholder here, not read from either camera's real
        descriptor -- this is diagnostic-only, `measure_translation_offset()`
        itself never uses it.

        Also tags EXPOSURE (seconds)/GAIN when known -- see
        `MountTestMovePanel.diagnostic_camera_state()`'s own docstring
        (real incidents ca728d27/0de26787): without this, whether
        auto-exposure changed gain *between* a step's before/after
        capture was a question the saved pixels alone couldn't answer.
        """
        camera_state = self._test_move_panel.diagnostic_camera_state()
        frames: list[Frame] = []
        for label, pixels in self._test_move_panel.diagnostic_frames().items():
            header = fits.Header()
            header["CALIBSRC"] = label
            state = camera_state.get(label)
            if state is not None:
                exposure_ms, gain = state
                header["EXPOSURE"] = exposure_ms / 1000.0
                header["GAIN"] = gain
            frames.append(
                Frame(
                    pixels=pixels.astype(np.float32),
                    header=header,
                    exposure_seconds=(state[0] / 1000.0) if state is not None else 0.0,
                    bit_depth=16,
                )
            )
        return frames

    def _diagnostic_images(self) -> dict[str, bytes]:
        images: dict[str, bytes] = {}
        for name, panel in (("left", self._left_panel), ("right", self._right_panel)):
            png_bytes = panel.displayed_image_png()
            if png_bytes is not None:
                images[f"{name}_display.png"] = png_bytes
        return images

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
        self._calibrate_fov_poll_timer.stop()
        self._left_panel.stop()
        self._right_panel.stop()
        self._focuser_panel.stop()
        self._main_filter_wheel_panel.stop()
        self._guide_filter_wheel_panel.stop()
        self._mount_panel.stop()
        self._test_move_panel.stop()
        self._fine_collimation_panel.stop()
        super().closeEvent(event)
