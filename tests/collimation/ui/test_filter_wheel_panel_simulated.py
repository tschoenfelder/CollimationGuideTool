"""Issue #51: FilterWheelPanel over the production `IndiFilterWheelAdapter`
on the simulated INDI filter wheel (fake time). Defect regression: a wheel
stuck Busy forever (diagnostic 73c7d59d, FilterWheelPanel half of 3fc09ae).

Seam note (#51 dependency): the panels time their confirmation safety net with
`time.monotonic()` directly (no clock seam), so stuck-Busy tests age the
issued-at timestamp the same way the existing panel tests do.
"""

from __future__ import annotations

from astrotool_core.testing import (
    FilterWheelScenario,
    make_simulated_filter_wheel_adapter,
)
from astrotool_core.timing import FakeClock
from collimation_tool.ui.filter_wheel_panel import FilterWheelPanel


class TestStuckBusyRecovers:
    """Defect class "stuck Busy" (diagnostic 73c7d59d, fixed by 3fc09ae): the
    device never leaves Busy, and the confirmation-timeout safety net used to
    be reachable only while NOT moving."""

    def test_a_filter_wheel_stuck_busy_forever_re_enables_the_selector(self, qapp: object) -> None:
        clock = FakeClock()
        adapter, wheel = make_simulated_filter_wheel_adapter(
            FilterWheelScenario("ToupTek EFW 2", slot_names=("L", "R", "G"), busy_forever=True),
            clock=clock,
        )
        panel = FilterWheelPanel(adapter, selectable=True)
        panel._connect_button.setChecked(True)
        panel._slot_combo.setCurrentIndex(2)
        panel._on_set_clicked()
        clock.advance(600.0)
        panel._poll_status()
        assert wheel.commanded_slots == [3] and wheel.state == "Busy"
        assert not panel._slot_combo.isEnabled()

        assert panel._slot_change_issued_at is not None
        panel._slot_change_issued_at -= 999.0
        panel._poll_status()

        assert panel._slot_combo.isEnabled()
        assert panel._set_button.isEnabled()
        panel._connect_button.setChecked(False)
