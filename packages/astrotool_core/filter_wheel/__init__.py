"""Filter wheel (EFW) port and adapters (no-op, fake, real INDI).

Issue #47: active slot commanding, on top of #34's read-only status display.
Separate device from the focuser/mount.
"""

from astrotool_core.filter_wheel.config import FilterWheelWiring, load_filter_wheel_wiring
from astrotool_core.filter_wheel.fake_filter_wheel import FakeFilterWheel
from astrotool_core.filter_wheel.indi_filter_wheel_adapter import IndiFilterWheelAdapter
from astrotool_core.filter_wheel.no_filter_wheel import NoFilterWheel
from astrotool_core.filter_wheel.port import FilterWheelPort, FilterWheelState

__all__ = [
    "FakeFilterWheel",
    "FilterWheelPort",
    "FilterWheelState",
    "FilterWheelWiring",
    "IndiFilterWheelAdapter",
    "NoFilterWheel",
    "load_filter_wheel_wiring",
]
