"""FakeFilterWheel — FilterWheelPort test/dev double with a fixed,
directly-configurable state. Issue #47: `set_slot()`/`begin_move()`/
`finish_move()` let a test drive a deterministic commanded-move sequence
without real timing/sleeps -- `begin_move()` puts it in the "moving" state a
real wheel would report while `finish_move()` lands on the new slot,
independently steppable so a panel test can observe the busy window."""

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
        filter_names: dict[int, str] | None = None,
    ) -> None:
        self._fail_connect = fail_connect
        self._available = available
        self._slot = slot
        self._filter_name = filter_name
        self._moving = moving
        self._filter_names = dict(filter_names or {})
        self._pending_slot: int | None = None

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
            filter_name=self._filter_name or self._filter_names.get(self._slot),
            moving=self._moving,
            reason=None,
        )

    def set_slot(self, slot: int) -> None:
        """Mirrors IndiFilterWheelAdapter.set_slot(): fire-and-forget, moves
        into the "moving" state immediately -- a test calls `finish_move()`
        (or `status()` directly checks `moving`) to observe/complete it, the
        same two-step shape a real command-then-poll caller sees."""
        if not self._available:
            return
        if self._moving:
            raise RuntimeError("FakeFilterWheel: a filter move is already in progress")
        self.begin_move(slot)

    def begin_move(self, slot: int) -> None:
        """Test helper: put the wheel in "moving toward `slot`" without
        landing on it yet -- lets a test observe the busy window."""
        self._moving = True
        self._pending_slot = slot

    def finish_move(self) -> None:
        """Test helper: complete whatever move `begin_move()`/`set_slot()`
        started."""
        if self._pending_slot is not None:
            self._slot = self._pending_slot
            self._filter_name = None  # a real device re-reports the name for the new slot
        self._pending_slot = None
        self._moving = False

    def slot_names(self) -> dict[int, str]:
        return dict(self._filter_names)
