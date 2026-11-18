"""Field incident 908a26b9 + USER DECISION 2026-11-17: when OnStep stopped answering, the app
reconnects the INDI driver automatically and the operator is told so on the Mount panel."""

from __future__ import annotations

import time

from astrotool_core.onstep import OnStepMountParkAdapter
from astrotool_core.testing.fake_onstep_indi_client import make_fake_onstep_indi_connection
from collimation_tool.ui.mount_park_panel import MountParkPanel


def _settle(panel: MountParkPanel) -> None:
    deadline = time.monotonic() + 5.0
    while panel._action_worker is not None:
        assert time.monotonic() < deadline, panel._status_label.text()
        time.sleep(0.02)
        panel._poll_status()
    panel._poll_status()


def test_an_automatic_reconnect_is_shown_on_the_status_line_until_the_next_action(
    qapp: object,
) -> None:
    connection, made = make_fake_onstep_indi_connection()
    panel = MountParkPanel(OnStepMountParkAdapter(connection))
    panel._connect_button.setChecked(True)
    client = made[0]
    client.snapshot_blockers = ("onstep_status_not_fresh",)
    client.status_age_ms = 25_000.0

    panel._on_unpark()
    _settle(panel)

    text = panel._status_label.text()
    assert text.startswith("Unparked")
    assert "OnStep had stopped answering (no status for 25 s)" in text
    assert "reconnected the INDI driver" in text
    assert client.reconnect_calls == 1
    panel._poll_status()  # the notice stays on the refreshed status line
    assert "reconnected the INDI driver" in panel._status_label.text()

    panel._on_park()
    _settle(panel)
    assert "reconnected" not in panel._status_label.text()
    panel.stop()
