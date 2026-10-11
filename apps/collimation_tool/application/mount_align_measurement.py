"""Mount Align measurement of one camera's before/after frames (S6.6a, moved out of the panel).

Star mode reduces a frame to its brightest source's centroid; terrestrial mode keeps the whole
frame for cross-correlation. No Qt.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
from astrotool_core.mount import AxisDirection, AxisResponse, MountAxis, response_from_positions
from astrotool_core.target.detector import detect_sources
from astrotool_core.target.translation_offset import measure_translation_offset

#: "star" measures a point-source centroid via detect_sources() (precise,
#: but needs an actual star -- see incident 6fa2aa59: correctly refuses
#: otherwise). "terrestrial" instead cross-correlates the whole before/
#: after frame via measure_translation_offset() -- works on any textured
#: scene (indoors, daytime), whole-pixel precision only. Issue #48: derived
#: from the global OperatingMode (Terrestrial -> "terrestrial", Astronomical ->
#: "star") -- Mount Align has no selector of its own.
TargetMode = Literal["star", "terrestrial"]

#: What a captured "before"/"after" measurement looks like in each mode --
#: a star's (x, y) centroid, or the whole mono frame to cross-correlate
#: against later.
Measurement = tuple[float, float] | np.ndarray


def measure_brightest_source(frame: np.ndarray | None) -> tuple[float, float] | None:
    if frame is None:
        return None
    result = detect_sources(frame)
    if not result.sources:
        return None
    brightest = max(result.sources, key=lambda source: source.peak)
    return (brightest.x, brightest.y)


def measure(mode: TargetMode, frame: np.ndarray | None) -> Measurement | None:
    """One camera's "before"/"after" measurement in the given mode -- a star centroid, or the
    whole frame itself (to cross-correlate against its counterpart later)."""
    if mode == "star":
        return measure_brightest_source(frame)
    return frame  # "terrestrial" -- any frame at all is usable here


def build_response(
    mode: TargetMode,
    axis: MountAxis,
    direction: AxisDirection,
    pulse_ms: int,
    before: Measurement,
    after: Measurement,
) -> AxisResponse | None:
    """Build one camera's `AxisResponse` from its before/after measurement -- star mode already
    has two absolute centroid positions to hand `response_from_positions()` directly;
    terrestrial mode's whole-frame correlation already *is* the displacement, so it hands that
    in as the "after" position relative to a `(0, 0)` "before" instead. Returns None if
    terrestrial mode's correlation didn't find enough shared structure to trust (see
    `measure_translation_offset`'s own docstring)."""
    if mode == "star":
        assert isinstance(before, tuple) and isinstance(after, tuple)
        return response_from_positions(axis, direction, pulse_ms, before, after)
    assert isinstance(before, np.ndarray) and isinstance(after, np.ndarray)
    offset = measure_translation_offset(before, after)
    if offset is None:
        return None
    return response_from_positions(
        axis, direction, pulse_ms, (0.0, 0.0), (offset.dx_px, offset.dy_px)
    )
