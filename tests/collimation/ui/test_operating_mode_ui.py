"""Issue #44 in the application: ONE explicit operating mode (Terrestrial /
Astronomical) owns the mount-tracking policy; terrestrial => tracking OFF on
connect, on mode change, after movement, and every terrestrial measurement path
is blocked (fail closed, explicit reason) while tracking cannot be disabled.

Issue #48 / S6.4 (+ user decision M04): that global mode is the ONLY Terrestrial/Astronomical
selector. Mount Align's measurement strategy and tracking need, and autofocus's metric, derive
from it; a mode change updates them at once; Natural vs Artificial Star stays an orthogonal
target type. S6.0c review P-a: entering Terrestrial while the mount is busy re-enforces once
the busy period ends."""

from __future__ import annotations

from typing import Any

import numpy as np
from astrotool_core.camera.replay_camera import ReplayCamera
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.mount.operating_mode import OperatingMode
from astrotool_core.mount.park_port import MountParkStatus
from astrotool_core.mount.tracking_mode import MOUNT_BUSY_REASON, TrackingMode
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from collimation_tool.application.autofocus_controller import AutofocusMode
from collimation_tool.domain.target_mode import CollimationTargetMode
from collimation_tool.ui.main_window import MainWindow
from PySide6.QtWidgets import QPushButton, QWidget


def _window(mount: FakeMountPark) -> MainWindow:
    image = np.full((60, 80), 100.0, dtype=np.float32)
    demo = ReplayCamera.from_arrays([image], cycle=True)
    return MainWindow(
        demo,
        device_lister=lambda: [],
        focuser=FakeFocuser(),
        mount=mount,
        pulse_mount=FakeMountAdapter(),
    )


def _tracking_mount(*, refuse_stop: bool = False) -> FakeMountPark:
    mount = FakeMountPark(start_parked=False, refuse_stop_tracking=refuse_stop)
    mount.start_tracking()
    return mount


class TestOperatingModeSelector:
    def test_default_mode_is_terrestrial_and_mount_align_measures_terrestrially(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))

        assert window._tracking_enforcer.mode is OperatingMode.TERRESTRIAL
        assert window._operating_terrestrial_button.isChecked()
        assert window._test_move_panel._target_mode() == "terrestrial"
        assert window._test_move_panel._required_tracking_mode() is TrackingMode.OFF

    def test_switching_to_astronomical_switches_mount_align_to_star_measurement(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))

        window._operating_astronomical_button.click()

        assert window._tracking_enforcer.mode is OperatingMode.ASTRONOMICAL
        assert window._test_move_panel._target_mode() == "star"
        assert window._test_move_panel._required_tracking_mode() is TrackingMode.ON
        assert window._star_field_mode_button.isChecked()

    def test_astronomical_to_terrestrial_forces_tracking_off(self, qapp: object) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        window._operating_astronomical_button.click()
        mount.start_tracking()
        assert mount.status().tracking is True

        window._operating_terrestrial_button.click()

        assert mount.status().tracking is False

    def test_terrestrial_to_astronomical_does_not_force_tracking(self, qapp: object) -> None:
        mount = FakeMountPark(start_parked=False)
        window = _window(mount)
        starts_before = mount.start_tracking_count

        window._operating_astronomical_button.click()

        assert mount.start_tracking_count == starts_before  # policy never forces ON

    def test_the_mode_switch_does_not_switch_away_an_artificial_star_registration_mode(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))
        window._artificial_star_mode_button.click()

        window._operating_terrestrial_button.click()

        assert window._artificial_star_mode_button.isChecked()


class TestConnectEnforcement:
    def test_connecting_the_mount_in_terrestrial_mode_turns_tracking_off(
        self, qapp: object
    ) -> None:
        mount = _tracking_mount()
        window = _window(mount)

        window._mount_panel._connect_button.setChecked(True)

        assert mount.status().tracking is False
        assert "tracking off" in window._mount_panel._status_label.text().lower()

    def test_connecting_in_astronomical_mode_leaves_tracking_alone(self, qapp: object) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        window._operating_astronomical_button.click()

        window._mount_panel._connect_button.setChecked(True)

        assert mount.status().tracking is True

    def test_tracking_that_cannot_be_disabled_is_shown_and_blocks_measurement(
        self, qapp: object
    ) -> None:
        mount = _tracking_mount(refuse_stop=True)
        window = _window(mount)

        window._mount_panel._connect_button.setChecked(True)

        text = window._mount_panel._status_label.text().lower()
        assert "blocked" in text and "tracking" in text
        assert window._tracking_enforcer.measurement_allowed() is False


class TestMeasurementPathsFailClosed:
    def test_fov_calibration_is_blocked_while_tracking_cannot_be_disabled(
        self, qapp: object
    ) -> None:
        window = _window(_tracking_mount(refuse_stop=True))

        window._calibrate_fov_button.click()

        text = window._calibrate_fov_status_label.text().lower()
        assert "blocked" in text and "tracking" in text

    def test_autofocus_is_blocked_while_tracking_cannot_be_disabled(self, qapp: object) -> None:
        window = _window(_tracking_mount(refuse_stop=True))

        window._focuser_panel._connect_button.setChecked(True)
        window._focuser_panel._on_auto_focus_clicked()

        text = window._focuser_panel._auto_focus_status_label.text().lower()
        assert "blocked" in text and "tracking" in text
        assert window._focuser_panel._autofocus_running is False

    def test_fine_collimation_is_blocked_while_tracking_cannot_be_disabled(
        self, qapp: object
    ) -> None:
        window = _window(_tracking_mount(refuse_stop=True))

        window._fine_collimation_panel._on_run_clicked()

        text = window._fine_collimation_panel._status_label.text().lower()
        assert "blocked" in text and "tracking" in text

    def test_measurement_paths_are_not_blocked_by_tracking_in_astronomical_mode(
        self, qapp: object
    ) -> None:
        window = _window(_tracking_mount(refuse_stop=True))
        window._operating_astronomical_button.click()

        window._calibrate_fov_button.click()

        assert "blocked" not in window._calibrate_fov_status_label.text().lower()

    def test_terrestrial_fov_calibration_turns_tracking_off_first(self, qapp: object) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        assert mount.status().tracking is True

        window._calibrate_fov_button.click()

        assert mount.status().tracking is False
        assert mount.start_tracking_count == 1  # only the fixture's own start


class TestMountAlignFollowsPolicy:
    def test_terrestrial_operating_mode_requires_tracking_off(self, qapp: object) -> None:
        """Migrated (#48): there is no local Star toggle left to flip; in Terrestrial the
        verification turns tracking OFF and never enables it."""
        mount = _tracking_mount()
        window = _window(mount)
        panel = window._test_move_panel

        failure = panel._verify_tracking_mode()

        assert failure is None
        assert mount.status().tracking is False
        assert mount.start_tracking_count == 1  # only the fixture's own start

    def test_a_star_capture_requested_before_a_switch_to_terrestrial_never_enables_tracking(
        self, qapp: object
    ) -> None:
        """#48 / #44: a capture request snapshotted in Astronomical (tracking ON required)
        whose tracking check runs after the user switched to Terrestrial must fail closed --
        the global mode, not the stale request, decides tracking."""
        mount = _tracking_mount()
        window = _window(mount)
        window._operating_astronomical_button.click()
        panel = window._test_move_panel
        request = panel._snapshot_capture_request("star", None, None, False)
        assert request.required_tracking is TrackingMode.ON
        window._operating_terrestrial_button.click()  # tracking turned OFF by the policy
        starts_before = mount.start_tracking_count

        result = panel._capture_blocking(request)

        assert mount.start_tracking_count == starts_before
        assert mount.status().tracking is False
        assert result.tracking_error is not None
        assert "terrestrial" in result.tracking_error.lower()

    def test_a_blocked_policy_is_reported_by_mount_align(self, qapp: object) -> None:
        window = _window(_tracking_mount(refuse_stop=True))

        failure = window._test_move_panel._verify_tracking_mode()

        assert failure is not None and "tracking" in failure.lower()


class TestDiagnostics:
    def test_diagnostics_expose_operating_mode_and_the_tracking_trail(self, qapp: object) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        window._mount_panel._connect_button.setChecked(True)

        context = window._diagnostic_context()

        assert context["operating_mode"] == "terrestrial"
        tracking = context["tracking_policy"]
        assert tracking["operating_mode"] == "terrestrial"
        contexts = [step["context"] for step in tracking["transitions"]]
        assert "connect" in contexts


def _checkable_button_texts(widget: QWidget) -> list[str]:
    return [b.text() for b in widget.findChildren(QPushButton) if b.isCheckable()]


class TestMountAlignHasNoLocalModeSelector:
    """#48 acceptance: Mount Align's Star/Terrestrial selector is removed; its measurement
    strategy derives from the global mode; no contradictory global/local state can exist."""

    def test_mount_align_exposes_no_star_or_terrestrial_selector(self, qapp: object) -> None:
        panel = _window(FakeMountPark(start_parked=False))._test_move_panel

        texts = _checkable_button_texts(panel)

        assert not any(t in ("Star", "Terrestrial") for t in texts), texts
        assert not hasattr(panel, "set_target_mode")  # no API to hold a local mode either

    def test_mount_align_reads_the_global_mode_owner_directly(self, qapp: object) -> None:
        """No copy that could go stale: whatever the single owner says is what Mount Align
        measures with -- even when the mode changes without the main window's push."""
        window = _window(FakeMountPark(start_parked=False))
        panel = window._test_move_panel

        window._tracking_enforcer.set_mode(OperatingMode.ASTRONOMICAL)
        assert (panel._target_mode(), panel._required_tracking_mode()) == (
            "star",
            TrackingMode.ON,
        )

        window._tracking_enforcer.set_mode(OperatingMode.TERRESTRIAL)
        assert (panel._target_mode(), panel._required_tracking_mode()) == (
            "terrestrial",
            TrackingMode.OFF,
        )

    def test_a_global_mode_change_updates_mount_align_without_restart(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))
        label = window._test_move_panel._measurement_mode_label

        assert "terrestrial" in label.text().lower()
        window._operating_astronomical_button.click()
        assert "star" in label.text().lower() and "terrestrial" not in label.text().lower()
        window._operating_terrestrial_button.click()
        assert "terrestrial" in label.text().lower()


class TestAutofocusFollowsTheGlobalMode:
    """User decision M04: the autofocus Star/Terrestrial selector is removed; the metric follows
    the global mode (Terrestrial -> Tenengrad, Astronomical -> star/FWHM). Artificial star stays
    a target type (from the collimation target mode)."""

    @staticmethod
    def _submitted_modes(window: MainWindow) -> list[AutofocusMode]:
        modes: list[AutofocusMode] = []

        def record(_controller: object, mode: AutofocusMode) -> bool:
            modes.append(mode)
            return False  # never actually start a run

        window._focuser_panel._autofocus_runner.submit = record  # type: ignore[method-assign,assignment]
        window._focuser_panel._connect_button.setChecked(True)
        return modes

    def test_autofocus_exposes_no_star_or_terrestrial_selector(self, qapp: object) -> None:
        panel = _window(FakeMountPark(start_parked=False))._focuser_panel

        texts = _checkable_button_texts(panel)

        assert not any(t in ("Star", "Terrestrial") for t in texts), texts

    def test_the_metric_follows_the_global_mode(self, qapp: object) -> None:
        window = _window(FakeMountPark(start_parked=False))
        modes = self._submitted_modes(window)
        try:
            window._focuser_panel._on_auto_focus_clicked()
            window._operating_astronomical_button.click()
            window._focuser_panel._on_auto_focus_clicked()
            window._operating_terrestrial_button.click()
            window._focuser_panel._on_auto_focus_clicked()
        finally:
            window._focuser_panel._connect_button.setChecked(False)

        assert modes == [AutofocusMode.TERRESTRIAL, AutofocusMode.STAR, AutofocusMode.TERRESTRIAL]

    def test_the_panel_says_which_metric_the_mode_selects(self, qapp: object) -> None:
        window = _window(FakeMountPark(start_parked=False))
        label = window._focuser_panel._af_metric_label

        assert "tenengrad" in label.text().lower()
        window._operating_astronomical_button.click()
        assert "fwhm" in label.text().lower()

    def test_an_artificial_star_target_coexists_with_terrestrial_mode(self, qapp: object) -> None:
        window = _window(FakeMountPark(start_parked=False))
        modes = self._submitted_modes(window)
        try:
            window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)
            window._focuser_panel._on_auto_focus_clicked()
        finally:
            window._focuser_panel._connect_button.setChecked(False)

        assert modes == [AutofocusMode.ARTIFICIAL_STAR]
        assert window._tracking_enforcer.mode is OperatingMode.TERRESTRIAL  # not overridden
        assert window._test_move_panel._target_mode() == "terrestrial"

    def test_a_natural_star_target_coexists_with_astronomical_mode(self, qapp: object) -> None:
        window = _window(FakeMountPark(start_parked=False))
        window._operating_astronomical_button.click()
        modes = self._submitted_modes(window)
        try:
            assert window._fine_collimation_panel.target_mode is CollimationTargetMode.NATURAL_STAR
            window._focuser_panel._on_auto_focus_clicked()
        finally:
            window._focuser_panel._connect_button.setChecked(False)

        assert modes == [AutofocusMode.STAR]
        assert window._tracking_enforcer.mode is OperatingMode.ASTRONOMICAL
        assert window._test_move_panel._target_mode() == "star"


class TestRegistrationChoicesFollowTheGlobalMode:
    """#48 audit + user decision B (2026-10-06): ASTAP star-field matching needs a star field,
    so it is restricted to Astronomical; NCC (texture correlation) stays available in both
    modes (Moon/planet texture at night)."""

    def test_terrestrial_mode_disables_the_star_field_choice(self, qapp: object) -> None:
        window = _window(FakeMountPark(start_parked=False))

        assert not window._star_field_mode_button.isEnabled()
        assert window._terrestrial_mode_button.isEnabled()
        assert window._artificial_star_mode_button.isEnabled()

    def test_astronomical_mode_keeps_ncc_and_enables_star_field(self, qapp: object) -> None:
        window = _window(FakeMountPark(start_parked=False))

        window._operating_astronomical_button.click()

        assert window._star_field_mode_button.isEnabled()
        assert window._terrestrial_mode_button.isEnabled()  # decision B: Moon/planet texture
        window._terrestrial_mode_button.click()
        assert window._terrestrial_mode_button.isChecked()
        assert window._tracking_enforcer.mode is OperatingMode.ASTRONOMICAL  # no mode change


class _BusyFakeMount(FakeMountPark):
    """Serves a held-over (not fresh) reading while `busy`, like the OnStep park adapter
    while a Mount Align GOTO holds the connection (S6.0c)."""

    def __init__(self) -> None:
        super().__init__(start_parked=False)
        self.busy = False

    def status(self) -> MountParkStatus:
        status = super().status()
        if self.busy:
            return MountParkStatus(status.available, status.parked, status.tracking, fresh=False)
        return status


class TestReenforceWhenTheBusyPeriodEnds:
    """S6.0c review P-a: switching to Terrestrial while the mount is busy shows the fail-closed
    BLOCKED state (unchanged) and turns tracking off by itself once the busy period ends."""

    def _switched_while_busy(self) -> tuple[_BusyFakeMount, MainWindow]:
        mount = _BusyFakeMount()
        window = _window(mount)
        window._operating_astronomical_button.click()
        mount.start_tracking()
        mount.busy = True
        window._operating_terrestrial_button.click()
        assert window._tracking_enforcer.last_gate.reason == MOUNT_BUSY_REASON
        assert "BLOCKED" in window._operating_status_label.text()
        return mount, window

    def test_while_still_busy_nothing_is_sent_and_the_gate_stays_closed(
        self, qapp: object
    ) -> None:
        mount, window = self._switched_while_busy()
        assert window._tracking_reenforce_timer.isActive()

        window._tracking_reenforce_timer.timeout.emit()

        assert mount.stop_tracking_count == 0
        assert window._tracking_enforcer.measurement_allowed() is False
        assert window._tracking_enforcer.reenforce_pending
        assert "BLOCKED" in window._operating_status_label.text()

    def test_when_the_busy_period_ends_tracking_is_turned_off_once(self, qapp: object) -> None:
        mount, window = self._switched_while_busy()
        mount.busy = False

        window._tracking_reenforce_timer.timeout.emit()

        assert mount.status().tracking is False and mount.stop_tracking_count == 1
        assert window._tracking_enforcer.measurement_allowed() is True
        assert "BLOCKED" not in window._operating_status_label.text()
        window._tracking_reenforce_timer.timeout.emit()  # one-shot: nothing more is sent
        assert mount.stop_tracking_count == 1

    def test_switching_back_to_astronomical_stops_the_pending_re_enforce(
        self, qapp: object
    ) -> None:
        mount, window = self._switched_while_busy()
        window._operating_astronomical_button.click()
        mount.busy = False

        window._tracking_reenforce_timer.timeout.emit()

        assert mount.status().tracking is True and mount.stop_tracking_count == 0


class TestDiagnosticsSeparateModeAndTargetType:
    def test_operating_mode_and_target_type_are_reported_independently(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))
        window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)

        context: dict[str, Any] = window._diagnostic_context()

        assert context["operating_mode"] == "terrestrial"
        assert context["target_mode"] == "artificial_star"
        mount_align = context["mount_test_move"]
        assert mount_align["operating_mode"] == "terrestrial"
        assert mount_align["measurement_mode"] == "terrestrial"
        assert context["focuser"]["autofocus_mode"] == "artificial_star"


def _artificial_star_with_astronomical(window: MainWindow) -> bool:
    """User decision A: the one combination that must never exist."""
    artificial = (
        window._fine_collimation_panel.target_mode is CollimationTargetMode.ARTIFICIAL_STAR
        or window._artificial_star_mode_button.isChecked()
    )
    return artificial and window._tracking_enforcer.mode is OperatingMode.ASTRONOMICAL


class TestArtificialStarImpliesTerrestrial:
    """User decision A (2026-10-06): an artificial star is a fixed target -- selecting it
    switches the global mode to Terrestrial (tracking OFF, visible note); switching to
    Astronomical returns the target to Natural star. Astronomical + Artificial star can't exist."""

    def test_selecting_an_artificial_star_target_switches_to_terrestrial(
        self, qapp: object
    ) -> None:
        mount = _tracking_mount()
        window = _window(mount)
        window._operating_astronomical_button.click()
        mount.start_tracking()

        window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)

        assert window._tracking_enforcer.mode is OperatingMode.TERRESTRIAL
        assert window._operating_terrestrial_button.isChecked()
        assert mount.status().tracking is False  # #44: tracking OFF with the mode
        assert "Artificial star is a fixed target" in window._operating_status_label.text()
        assert window._fine_collimation_panel.target_mode is CollimationTargetMode.ARTIFICIAL_STAR
        assert not _artificial_star_with_astronomical(window)

    def test_choosing_artificial_star_registration_switches_to_terrestrial(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))
        window._operating_astronomical_button.click()

        window._artificial_star_mode_button.click()

        assert window._tracking_enforcer.mode is OperatingMode.TERRESTRIAL
        assert window._artificial_star_mode_button.isChecked()  # the choice is kept
        assert "Artificial star is a fixed target" in window._operating_status_label.text()
        assert not _artificial_star_with_astronomical(window)

    def test_switching_to_astronomical_returns_the_target_to_natural_star(
        self, qapp: object
    ) -> None:
        window = _window(FakeMountPark(start_parked=False))
        window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)
        window._artificial_star_mode_button.click()

        window._operating_astronomical_button.click()

        assert window._tracking_enforcer.mode is OperatingMode.ASTRONOMICAL
        assert window._fine_collimation_panel.target_mode is CollimationTargetMode.NATURAL_STAR
        assert not window._artificial_star_mode_button.isChecked()
        assert window._focuser_panel._autofocus_mode() is AutofocusMode.STAR
        assert "natural star" in window._operating_status_label.text().lower()
        assert not _artificial_star_with_astronomical(window)

    def test_a_manual_mode_change_clears_the_note(self, qapp: object) -> None:
        window = _window(FakeMountPark(start_parked=False))
        window._operating_astronomical_button.click()
        window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)
        assert "Artificial star is a fixed target" in window._operating_status_label.text()

        window._fine_collimation_panel.set_target_mode(CollimationTargetMode.NATURAL_STAR)
        window._operating_astronomical_button.click()
        window._operating_terrestrial_button.click()

        assert "Artificial star is a fixed target" not in window._operating_status_label.text()

    def test_a_busy_mount_keeps_the_gate_closed_and_re_enforces_when_free(
        self, qapp: object
    ) -> None:
        mount = _BusyFakeMount()
        window = _window(mount)
        window._operating_astronomical_button.click()
        mount.start_tracking()
        mount.busy = True

        window._fine_collimation_panel.set_target_mode(CollimationTargetMode.ARTIFICIAL_STAR)

        assert window._tracking_enforcer.mode is OperatingMode.TERRESTRIAL
        assert window._tracking_enforcer.last_gate.reason == MOUNT_BUSY_REASON
        assert mount.stop_tracking_count == 0
        mount.busy = False
        window._tracking_reenforce_timer.timeout.emit()
        assert mount.status().tracking is False and mount.stop_tracking_count == 1
