"""The operator's explicit "Confirm at home" step (OnStepAdapter refuses all motion until then)."""

from __future__ import annotations

from astrotool_core.onstep import OnStepMountParkAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.fake_onstep_client import make_fake_onstep_connection
from collimation_tool.ui.mount_park_panel import MountParkPanel


def test_a_port_without_the_concept_shows_no_confirm_button(qapp: object) -> None:
    panel = MountParkPanel(FakeMountPark())
    assert panel._confirm_home_button.isHidden()
    assert panel._home_confirmed() is None
    panel.stop()


def test_the_status_says_home_is_not_confirmed_until_the_operator_confirms(
    qapp: object,
) -> None:
    connection = make_fake_onstep_connection()
    adapter = OnStepMountParkAdapter(connection)
    panel = MountParkPanel(adapter)
    panel._connect_button.setChecked(True)
    assert not panel._confirm_home_button.isHidden()
    assert "HOME NOT CONFIRMED" in panel._status_label.text()

    panel._confirm_home_button.click()

    assert "Home position confirmed" in panel._status_label.text()
    panel._poll_status()
    assert "home confirmed" in panel._status_label.text()
    assert adapter.home_confirmed
    panel.stop()


def test_a_failed_confirmation_is_shown_not_swallowed(qapp: object) -> None:
    adapter = OnStepMountParkAdapter(make_fake_onstep_connection())
    panel = MountParkPanel(adapter)  # never connected: the adapter cannot confirm
    panel._on_confirm_home()
    assert "Confirm home failed" in panel._status_label.text()
    panel.stop()
