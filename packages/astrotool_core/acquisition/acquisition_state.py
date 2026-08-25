"""AcquisitionState — lifecycle states shared by single-shot and streaming capture."""

from __future__ import annotations

from enum import Enum, auto


class AcquisitionState(Enum):
    IDLE = auto()
    CAPTURING = auto()
    STREAMING = auto()
    ABORTED = auto()
    ERROR = auto()
