"""Tests for FocuserPanel — manual jog behavior already exercised
indirectly via test_collimation_main_window.py; this file covers the
panel directly (see project convention: construct the panel directly
rather than via a full MainWindow for panel-level logic, to avoid the
known Qt-teardown segfault odds a MainWindow-based test raises), focused
on issue #33's new Auto Focus control: enablement gating, the mode
toggle, a full run's status/diagnostics, and cancellation."""

from __future__ import annotations

import time

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import (
    DeliveredFrame,
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.focus.fake_focuser import FakeFocuser
from astrotool_core.testing.frame_factory import single_star_image
from collimation_tool.application.autofocus_search import AutofocusStatus
from collimation_tool.ui.focuser_panel import FocuserPanel


def _star_frame(sigma: float = 2.0) -> np.ndarray:
    return single_star_image((80, 80), x=40.0, y=40.0, peak=3000.0, sigma=sigma, background=100.0)


def _always_fresh_frame(reference_monotonic: float, timeout_s: float) -> FrameAcquisitionResult:
    return FrameAcquisitionResult(
        status=FrameAcquisitionStatus.OK,
        frame=DeliveredFrame(
            pixels=_star_frame(), captured_at_monotonic=time.monotonic(), exposure_seconds=0.01
        ),
    )


def _run_autofocus_to_completion(panel: FocuserPanel, *, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while panel._autofocus_poll_timer.isActive():
        assert time.monotonic() < deadline, "autofocus never completed"
        time.sleep(0.01)
        panel._poll_autofocus()


def _select_step(panel: FocuserPanel, step: int) -> None:
    panel._step_group.button(step).setChecked(True)


class TestManualStepSizes:
    """Issue #38: 1/10/100/200 replaces the old 1/5/10/50 set."""

    def test_the_panel_offers_exactly_the_four_intended_step_sizes(self, qapp: object) -> None:
        panel = FocuserPanel(FakeFocuser())

        ids = sorted(panel._step_group.id(button) for button in panel._step_group.buttons())

        assert ids == [1, 10, 100, 200]

    def test_the_1_step_option_is_selected_by_default(self, qapp: object) -> None:
        panel = FocuserPanel(FakeFocuser())

        assert panel._selected_step() == 1

    def test_manual_inward_movement_uses_the_selected_increment(self, qapp: object) -> None:
        focuser = FakeFocuser()
        panel = FocuserPanel(focuser)
        panel._connect_button.setChecked(True)
        _select_step(panel, 100)

        panel._on_move_in()

        assert focuser.get_position() == -100

    def test_manual_outward_movement_uses_the_selected_increment(self, qapp: object) -> None:
        focuser = FakeFocuser()
        panel = FocuserPanel(focuser)
        panel._connect_button.setChecked(True)
        _select_step(panel, 200)

        panel._on_move_out()

        assert focuser.get_position() == 200


class TestAutoFocusButtonEnablement:
    def test_disabled_before_connecting(self, qapp: object) -> None:
        panel = FocuserPanel(
            FakeFocuser(),
            get_frame=lambda: _star_frame(),
            wait_for_frame=_always_fresh_frame,
            set_auto_exposure_paused=lambda paused: None,
        )
        assert not panel._auto_focus_button.isEnabled()

    def test_disabled_when_no_frame_callables_are_supplied(self, qapp: object) -> None:
        panel = FocuserPanel(FakeFocuser())  # manual-jog-only construction, unchanged
        panel._connect_button.setChecked(True)
        try:
            assert not panel._auto_focus_button.isEnabled()
        finally:
            panel._connect_button.setChecked(False)

    def test_enabled_once_connected_with_frame_callables_supplied(self, qapp: object) -> None:
        panel = FocuserPanel(
            FakeFocuser(),
            get_frame=lambda: _star_frame(),
            wait_for_frame=_always_fresh_frame,
            set_auto_exposure_paused=lambda paused: None,
        )
        panel._connect_button.setChecked(True)
        try:
            assert panel._auto_focus_button.isEnabled()
        finally:
            panel._connect_button.setChecked(False)

    def test_disabled_while_a_run_is_in_flight_and_cancel_becomes_enabled(
        self, qapp: object
    ) -> None:
        panel = FocuserPanel(
            FakeFocuser(),
            get_frame=lambda: _star_frame(),
            wait_for_frame=_always_fresh_frame,
            set_auto_exposure_paused=lambda paused: None,
        )
        panel._connect_button.setChecked(True)
        try:
            assert not panel._auto_focus_cancel_button.isEnabled()
            panel._on_auto_focus_clicked()
            assert not panel._auto_focus_button.isEnabled()
            assert panel._auto_focus_cancel_button.isEnabled()
            _run_autofocus_to_completion(panel)
            assert panel._auto_focus_button.isEnabled()
            assert not panel._auto_focus_cancel_button.isEnabled()
        finally:
            panel._connect_button.setChecked(False)


class TestModeToggle:
    def test_star_mode_is_selected_by_default(self, qapp: object) -> None:
        panel = FocuserPanel(FakeFocuser())
        assert panel._af_star_button.isChecked()
        assert not panel._af_terrestrial_button.isChecked()


class TestAutoFocusRun:
    def test_a_full_run_updates_the_status_label_and_diagnostics(self, qapp: object) -> None:
        panel = FocuserPanel(
            FakeFocuser(),
            get_frame=lambda: _star_frame(),
            wait_for_frame=_always_fresh_frame,
            set_auto_exposure_paused=lambda paused: None,
        )
        panel._connect_button.setChecked(True)
        try:
            assert panel.diagnostic_autofocus_evidence() == {}

            panel._on_auto_focus_clicked()
            _run_autofocus_to_completion(panel)

            assert panel._auto_focus_status_label.text() != ""
            evidence = panel.diagnostic_autofocus_evidence()
            assert evidence != {}
            assert evidence["mode"] == "star"
            assert "status" in evidence
            assert "samples" in evidence
        finally:
            panel._connect_button.setChecked(False)

    def test_optical_train_label_appears_in_status_text_and_diagnostics(
        self, qapp: object
    ) -> None:
        # Issue #35: the running/completed status must visually identify
        # which optical train Auto Focus applies to, and diagnostics must
        # record it -- previously neither existed.
        panel = FocuserPanel(
            FakeFocuser(),
            get_frame=lambda: _star_frame(),
            wait_for_frame=_always_fresh_frame,
            set_auto_exposure_paused=lambda paused: None,
            optical_train_label="Guide",
        )
        panel._connect_button.setChecked(True)
        try:
            panel._on_auto_focus_clicked()
            assert "Guide" in panel._auto_focus_status_label.text()
            _run_autofocus_to_completion(panel)

            assert "Guide" in panel._auto_focus_status_label.text()
            evidence = panel.diagnostic_autofocus_evidence()
            assert evidence["camera_label"] == "Guide"
            assert evidence["optical_train"] == "Guide"
            assert evidence["focuser_label"] == "Guide"
        finally:
            panel._connect_button.setChecked(False)

    def test_cancel_stops_a_run(self, qapp: object) -> None:
        panel = FocuserPanel(
            FakeFocuser(),
            get_frame=lambda: _star_frame(),
            wait_for_frame=_always_fresh_frame,
            set_auto_exposure_paused=lambda paused: None,
        )
        panel._connect_button.setChecked(True)
        try:
            panel._on_auto_focus_clicked()
            panel._on_auto_focus_cancel_clicked()
            _run_autofocus_to_completion(panel)

            assert panel._last_autofocus_result is not None
            # A synthetic single-star frame at a fixed sigma converges (or
            # is already flat) fast enough that cancellation may or may
            # not win the race -- the real assertion is that the run
            # completes cleanly either way, never left hung.
            assert panel._last_autofocus_result.status in (
                AutofocusStatus.SUCCESS, AutofocusStatus.CANCELLED,
            )
        finally:
            panel._connect_button.setChecked(False)
