"""DriftEstimator — rolling RMS of guide-error magnitude.

Extracted from smart_telescope's `services/guiding_service.py::_loop` (the
inline `error_history`/`rms_px` rolling-window calculation) into its own
reusable domain type — no closer existing analog was found during Stage 6
research.
"""

from __future__ import annotations

from collections import deque


class DriftEstimator:
    """Tracks a rolling window of guide-error magnitudes and reports RMS drift."""

    def __init__(self, window: int = 10) -> None:
        self._history: deque[float] = deque(maxlen=window)

    def add(self, error_magnitude_px: float) -> None:
        self._history.append(error_magnitude_px)

    def rms_px(self) -> float:
        if len(self._history) < 2:
            return 0.0
        result: float = (sum(e**2 for e in self._history) / len(self._history)) ** 0.5
        return result

    def reset(self) -> None:
        self._history.clear()
