"""FakeFilterWheel — FilterWheelPort test/dev double with a fixed,
directly-configurable state (no simulated movement -- issue #34 is
display-only, there is no move to simulate)."""

from __future__ import annotations

from astrotool_core.filter_wheel.port import FilterWheelPort, FilterWheelState


class FakeFilterWheel(FilterWheelPort):
    def __init__(
        self,
        *,
        fail_connect: bool = False,
        available: bool = True,
        slot: int = 1,
        filter_name: str | None = None,
        moving: bool = False,
    ) -> None:
        self._fail_connect = fail_connect
        self._available = available
        self._slot = slot
        self._filter_name = filter_name
        self._moving = moving

    def connect(self) -> None:
        if self._fail_connect:
            raise ConnectionError("FakeFilterWheel: connect failed (simulated)")

    def disconnect(self) -> None:
        pass

    @property
    def is_available(self) -> bool:
        return self._available

    def status(self) -> FilterWheelState:
        if not self._available:
            return FilterWheelState(
                available=False,
                current_slot=None,
                filter_name=None,
                moving=False,
                reason="no filter wheel detected",
            )
        return FilterWheelState(
            available=True,
            current_slot=self._slot,
            filter_name=self._filter_name,
            moving=self._moving,
            reason=None,
        )
