"""Hold-to-move on the Mount Align RA/Dec pad (Torsten, 2026-11-08) over the production
`OnStepMountPulseAdapter` and the fake OnStep INDI client: a held button jogs its axis while
it stays pressed (OnStepAdapter's jog and deadman), a quick click is still a nudge, and RA
waits for Dec after connecting (OnStepAdapter's DEC-first rule)."""

from __future__ import annotations

from astrotool_core.mount import AxisDirection, JogOutcome, MountAxis
from astrotool_core.mount.port import RA_NEEDS_DEC_FIRST
from collimation_tool.ui.mount_hold_jog import JOG_OWNER, HoldToJog
from PySide6.QtWidgets import QPushButton
from test_mount_align_real_onstep_adapter import _drain, _Rig

POS, NEG = AxisDirection.POSITIVE, AxisDirection.NEGATIVE


def _unparked() -> _Rig:
    """A jog never unparks (unlike a nudge's runner): the mount must already be unparked."""
    rig = _Rig()
    rig.client.parked = False
    return rig


def _hold(panel: object, label: str, camera: str = "left") -> tuple[HoldToJog, QPushButton]:
    """Press `label` and let the hold threshold pass (the timer's slot, called directly)."""
    hold: HoldToJog = panel._hold_jog  # type: ignore[attr-defined]
    button: QPushButton = panel._nudge_buttons[camera][label]  # type: ignore[attr-defined]
    button.setDown(True)
    button.pressed.emit()
    hold._on_hold()
    return hold, button


def _let_go(hold: HoldToJog, button: QPushButton) -> None:
    button.setDown(False)
    button.released.emit()


class TestHoldToMove:
    def test_a_held_button_jogs_its_axis_until_it_is_released(self, qapp: object) -> None:
        rig = _unparked()
        panel = rig.panel()
        hold, button = _hold(panel, "Dec +")

        assert hold.active and len(rig.client.jogs) == 1
        jog = rig.client.jogs[0]
        assert (jog.axis, jog.direction) == ("dec", 1)
        assert panel._motion_executor.in_flight == JOG_OWNER
        assert "while the button is held" in panel._result_label.text()
        for _ in range(5):
            hold._on_tick()  # the renew timer
        assert jog.renewals == 5
        assert rig.client.dec_deg > rig.dec0
        assert button.isEnabled()  # the held button is never disabled under the finger
        assert not panel._run_calibration_button.isEnabled()

        _let_go(hold, button)

        assert jog.ended_by == "released"
        assert not hold.active and panel._motion_executor.in_flight is None
        assert "Moved Dec-axis + (released)" in panel._result_label.text()
        assert rig.client.axis_move_calls == []  # no finite nudge was sent
        panel.stop()

    def test_a_quick_click_is_still_a_nudge(self, qapp: object) -> None:
        rig = _Rig()
        panel = rig.panel()
        panel._nudge_buttons["left"]["Dec +"].click()
        _drain(panel)
        assert rig.client.jogs == []
        assert [axis for axis, _offset in rig.client.axis_move_calls] == ["dec"]
        panel.stop()

    def test_a_button_that_is_no_longer_down_releases_the_jog(self, qapp: object) -> None:
        rig = _unparked()
        panel = rig.panel()
        hold, button = _hold(panel, "Dec -")
        button.setDown(False)  # e.g. disabled or the mouse left: no released() arrives
        hold._on_tick()
        assert rig.client.jogs[0].ended_by == "released"
        assert not hold.active
        panel.stop()

    def test_a_jog_that_ended_by_itself_is_reported(self, qapp: object) -> None:
        rig = _unparked()
        panel = rig.panel()
        hold, button = _hold(panel, "Dec +")
        rig.client.jogs[0].expire()  # OnStepAdapter's deadman
        hold._on_tick()
        hold._on_tick()
        assert not hold.active
        assert "not renewed in time" in panel._result_label.text()
        _let_go(hold, button)  # the late release changes nothing
        assert not hold.active
        panel.stop()

    def test_stop_ends_the_jog_with_the_emergency_stop(self, qapp: object) -> None:
        rig = _unparked()
        panel = rig.panel()
        hold, _button = _hold(panel, "Dec +")
        panel._on_stop()
        deadline_ticks = 200
        while hold.active and deadline_ticks:
            hold._on_tick()
            deadline_ticks -= 1
        assert rig.client.jogs[0].done
        assert not hold.active and panel._motion_executor.in_flight is None
        assert "stopped by the user" in panel._result_label.text()
        panel.stop()

    def test_a_refused_jog_releases_its_claim_and_sends_no_click(self, qapp: object) -> None:
        rig = _Rig()
        panel = rig.panel()
        rig.client.at_limit = True
        hold, button = _hold(panel, "Dec +")
        assert not hold.active and panel._motion_executor.in_flight is None
        assert "Move failed" in panel._result_label.text()
        _let_go(hold, button)
        assert rig.client.axis_move_calls == []  # the refused hold is not turned into a nudge
        panel.stop()


    def test_a_parked_mount_is_refused_with_the_reason(self, qapp: object) -> None:
        rig = _Rig()  # parked
        panel = rig.panel()
        hold, button = _hold(panel, "Dec +")
        assert not hold.active and rig.client.jogs == []
        assert "Move failed" in panel._result_label.text()
        _let_go(hold, button)
        panel.stop()


class TestDecFirst:
    def test_ra_buttons_wait_for_dec_after_connecting(self, qapp: object) -> None:
        rig = _unparked()
        rig.client.enforce_dec_first = True
        rig.client.ra_axis_defined = False
        panel = rig.panel()
        panel._update_buttons_enabled()
        pad = panel._nudge_buttons["left"]
        assert not pad["RA +"].isEnabled() and not pad["RA -"].isEnabled()
        assert pad["RA +"].toolTip() == RA_NEEDS_DEC_FIRST
        assert pad["Dec +"].isEnabled() and pad["Dec -"].isEnabled()

        hold, button = _hold(panel, "Dec +")
        hold._on_tick()
        _let_go(hold, button)

        assert pad["RA +"].isEnabled() and pad["RA +"].toolTip() == ""
        panel.stop()

    def test_the_adapter_refuses_an_ra_jog_before_dec(self, qapp: object) -> None:
        rig = _Rig()
        rig.client.enforce_dec_first = True
        rig.client.ra_axis_defined = False
        rig.client.parked = False
        result = rig.mount.start_jog(MountAxis.AXIS1, POS)
        assert not result.accepted and "move the DEC axis first" in result.message
        assert rig.client.jogs == []


class TestAdapterJog:
    def test_outcome_is_none_while_running_then_says_how_it_ended(self, qapp: object) -> None:
        rig = _Rig()
        rig.client.parked = False
        assert rig.mount.supports_jog
        assert rig.mount.start_jog(MountAxis.AXIS2, NEG).accepted
        assert rig.mount.jog_outcome() is None
        assert rig.mount.renew_jog()
        assert not rig.mount.start_jog(MountAxis.AXIS2, NEG).accepted  # one at a time
        rig.mount.release_jog()
        assert rig.mount.jog_outcome() == JogOutcome(ok=True, ended_by="released")
        assert not rig.mount.renew_jog()

    def test_a_latched_stop_refuses_a_jog_unsent(self, qapp: object) -> None:
        rig = _Rig()
        rig.client.parked = False
        rig.mount.abort()
        result = rig.mount.start_jog(MountAxis.AXIS2, POS)
        assert not result.accepted and "stopped by the user" in result.message
        assert rig.client.jogs == []

    def test_disconnect_releases_a_running_jog(self, qapp: object) -> None:
        rig = _Rig()
        rig.client.parked = False
        assert rig.mount.start_jog(MountAxis.AXIS2, POS).accepted
        rig.mount.disconnect()
        assert rig.client.jogs[0].ended_by == "released"
