"""Issue #51: FocuserPanel over the production `OnStepFocuserAdapter` +
`OnStepConnection` on the simulated OnStep controller (fake time). Defect
regressions: connect-error variants (diagnostic 11e564ad, 7bb7b68) and a
focuser stuck Busy forever (diagnostic 73c7d59d, FocuserPanel half of 3fc09ae).

Seam note (#51 dependency): the panels time their confirmation safety net with
`time.monotonic()` directly (no clock seam), so stuck-Busy tests age the
issued-at timestamp the same way the existing panel tests do.
"""

from __future__ import annotations

import pytest
from astrotool_core.acquisition.stable_frame_acquisition import (
    FrameAcquisitionResult,
    FrameAcquisitionStatus,
)
from astrotool_core.onstep import (
    OnStepFocuserAdapter,
)
from astrotool_core.testing import (
    FocuserScenario,
    OnStepScenario,
    SimulatedOnStepIndiClient,
    make_simulated_onstep_connection,
)
from astrotool_core.timing import FakeClock
from collimation_tool.ui.focuser_panel import FocuserPanel


def _focuser_panel(
    scenario: OnStepScenario, clock: FakeClock
) -> tuple[FocuserPanel, list[SimulatedOnStepIndiClient]]:
    connection, made = make_simulated_onstep_connection(scenario, clock=clock)
    panel = FocuserPanel(
        OnStepFocuserAdapter(connection),
        get_frame=lambda: None,
        wait_for_frame=lambda _reference, _timeout: FrameAcquisitionResult(
            FrameAcquisitionStatus.TIMEOUT
        ),
        set_auto_exposure_paused=lambda _paused: None,
    )
    return panel, made


class TestConnectErrorVariantsAreShownNotRaised:
    """Defect class "connect timeout" (diagnostic 11e564ad, fixed by 7bb7b68):
    `OnStepConnection.acquire()` propagates TimeoutError/RuntimeError/
    ValueError as well as ConnectionError, and FocuserPanel used to catch only
    ConnectionError -- an unhandled TimeoutError on Connect. Here the real
    error shapes come from the simulated controller through the production
    connection and adapter, and the panel must recover once the fault clears."""

    @pytest.mark.parametrize(
        "error",
        [
            TimeoutError("INDI property LX200 OnStep.CONNECTION did not update"),
            RuntimeError("INDI client is already connected; close before reconnecting"),
            ValueError("Invalid isoformat string: ''"),
            ConnectionError("OnStep INDI device is not connected"),
            KeyError("UTC"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_the_failure_is_shown_and_a_retry_connects(
        self, qapp: object, error: BaseException
    ) -> None:
        panel, _ = _focuser_panel(OnStepScenario(connect_errors=[error]), FakeClock())

        panel._connect_button.setChecked(True)

        assert "Connect failed" in panel._status_label.text()
        assert str(error) in panel._status_label.text()
        assert not panel._connect_button.isChecked()
        assert panel.diagnostic_context()["last_connect_error"] == str(error)

        panel._connect_button.setChecked(True)  # the controller answers now
        assert panel._status_label.text() == "Position 5000 / 10000"
        panel.stop()


class TestStuckBusyRecovers:
    """Defect class "stuck Busy" (diagnostic 73c7d59d, fixed by 3fc09ae): the
    device never leaves Busy, and the confirmation-timeout safety net used to
    be reachable only while NOT moving."""

    def test_a_focuser_stuck_busy_forever_releases_the_move_lock_after_the_timeout(
        self, qapp: object
    ) -> None:
        """The FocuserPanel half of 3fc09ae, which proofs/diag-73c7d59d lists
        as having no regression test. In/Out stay disabled while the driver
        says it moves (they also check is_moving()), but the panel's own
        in-flight lock must clear so Auto Focus is offered again."""
        clock = FakeClock()
        panel, made = _focuser_panel(
            OnStepScenario(focuser=FocuserScenario(busy_forever=True)), clock
        )
        panel._connect_button.setChecked(True)
        assert panel._auto_focus_button.isEnabled()

        panel._on_move_out()  # the simulated driver goes Busy and stays there
        assert clock.monotonic() == 35.0  # 0.4.1: 30 s move timeout + 5 s stop wait, fake time
        panel._poll_status()
        assert panel._move_in_flight and not panel._auto_focus_button.isEnabled()

        assert panel._move_issued_at is not None
        panel._move_issued_at -= 999.0  # the panel's real-clock safety net elapses
        panel._poll_status()

        assert made[0].focuser.moving is True  # still stuck
        assert panel._move_in_flight is False
        assert panel._auto_focus_button.isEnabled()
        panel.stop()
