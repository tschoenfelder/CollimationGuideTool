"""Session context, structured event logging, and frame recording for
replay/golden-master testing.
"""

from astrotool_core.session.event_log import EventLogger, EventRecord
from astrotool_core.session.frame_recorder import save_frame
from astrotool_core.session.session_context import SessionContext

__all__ = [
    "EventLogger",
    "EventRecord",
    "SessionContext",
    "save_frame",
]
