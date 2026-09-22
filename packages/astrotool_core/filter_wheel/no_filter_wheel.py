"""NoFilterWheel — FilterWheelPort stand-in for "no filter wheel configured"."""

from __future__ import annotations

from astrotool_core.filter_wheel.port import FilterWheelPort, FilterWheelState


class NoFilterWheel(FilterWheelPort):
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return False

    def status(self) -> FilterWheelState:
        return FilterWheelState(
            available=False,
            current_slot=None,
            filter_name=None,
            moving=False,
            reason="not connected",
        )

    def set_slot(self, slot: int) -> None:
        pass  # no wheel configured -- always a safe no-op
