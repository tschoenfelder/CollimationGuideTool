"""Session context, structured event logging, frame recording for
replay/golden-master testing, and the device connection lifecycle shared by
both apps (issue #52, `device_connection`).
"""

from astrotool_core.session.device_connection import (
    INTERNAL_ERRORS,
    LIFECYCLE_FAILURES,
    ConnectableDevice,
    ConnectionFailure,
    ConnectionPhase,
    DeviceConnectionService,
    SwitchResult,
    close_device,
    describe_failure,
    open_device,
    run_phase,
    stop_each,
    switch_device,
)
from astrotool_core.session.event_log import EventLogger, EventRecord
from astrotool_core.session.frame_recorder import save_frame
from astrotool_core.session.session_context import SessionContext

__all__ = [
    "ConnectableDevice",
    "ConnectionFailure",
    "ConnectionPhase",
    "DeviceConnectionService",
    "EventLogger",
    "EventRecord",
    "INTERNAL_ERRORS",
    "LIFECYCLE_FAILURES",
    "SessionContext",
    "SwitchResult",
    "close_device",
    "describe_failure",
    "open_device",
    "run_phase",
    "save_frame",
    "stop_each",
    "switch_device",
]
