"""CollimationRecenterPolicy — drives a measured pixel offset to zero via
pulse-guide corrections.

Redesigned (not a literal port) from smart_telescope's
`services/collimation/mount_centering.py::PulseCenterer`. The old
implementation derived pulse duration from a theoretical sidereal-rate
constant (`pixel_scale_arcsec` + a guide-rate fraction) and called the old
MountPort's `guide(direction: str, duration_ms)` using an assumed image-
orientation sign convention (`dx>0 -> "w"`, `dy>0 -> "n"`). The new
MountPort only has `pulse_axis(axis, direction, duration_ms)`; rather than
reintroduce sidereal-rate arithmetic or an assumed orientation, this
policy uses Stage 4's empirically measured `CalibrationMatrix` directly:
`duration_ms = |offset_component_px| / measured_px_per_ms`, and picks
whichever `AxisDirection`'s calibrated response vector actually opposes
the measured error (by its measured sign, not an assumed convention).

Tolerance/settle/divergence-guard fields port near-verbatim from
`MountCenteringConfig`. One behavior change from the source (see
docs/porting-notes.md): a rejected pulse now aborts immediately instead
of being silently ignored — the original never checked `guide()`'s
returned bool.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from astrotool_core.mount.axis_calibration import CalibrationMatrix
from astrotool_core.mount.port import AxisDirection, MountAxis, MountPort
from astrotool_core.target.roi_tracker import TrackingResult, TrackingState

_LOCKED_STATES = (TrackingState.LOCKED, TrackingState.REACQUIRED)


@dataclass(frozen=True)
class RecenterConfig:
    max_pulse_ms: int = 500
    settle_ms: int = 750
    fine_tolerance_px: float = 5.0
    rough_tolerance_px: float = 20.0
    max_iterations: int = 30
    max_diverge_count: int = 3


@dataclass(frozen=True)
class MountCorrectionResult:
    """reason is one of: within_tolerance, star_lost, diverging,
    pulse_rejected, max_pulses, cancelled."""

    success: bool
    pulses_issued: int
    final_offset_px: float
    reason: str


class CollimationRecenterPolicy:
    """Iteratively pulses the mount to drive a measured offset toward (0, 0)."""

    def __init__(
        self,
        mount: MountPort,
        calibration: CalibrationMatrix,
        config: RecenterConfig | None = None,
    ) -> None:
        self._mount = mount
        self._calibration = calibration
        self._config = config or RecenterConfig()

    def center(
        self,
        measure: Callable[[], TrackingResult],
        reference: tuple[float, float],
        *,
        cancel_check: Callable[[], bool] | None = None,
    ) -> MountCorrectionResult:
        cfg = self._config
        ref_x, ref_y = reference
        pulses = 0
        prev_dist: float | None = None
        diverge_count = 0

        for _ in range(cfg.max_iterations):
            if cancel_check is not None and cancel_check():
                return MountCorrectionResult(False, pulses, prev_dist or 0.0, "cancelled")

            result = measure()
            if result.state not in _LOCKED_STATES or result.x is None or result.y is None:
                return MountCorrectionResult(False, pulses, 999.0, "star_lost")

            dx = result.x - ref_x
            dy = result.y - ref_y
            dist = (dx**2 + dy**2) ** 0.5
            if dist <= cfg.fine_tolerance_px:
                return MountCorrectionResult(True, pulses, dist, "within_tolerance")

            if prev_dist is not None and dist > prev_dist * 1.1:
                diverge_count += 1
                if diverge_count >= cfg.max_diverge_count:
                    return MountCorrectionResult(False, pulses, dist, "diverging")
            else:
                diverge_count = max(0, diverge_count - 1)
            prev_dist = dist

            axis = MountAxis.AXIS1 if abs(dx) >= abs(dy) else MountAxis.AXIS2
            offset_component = dx if axis is MountAxis.AXIS1 else dy
            direction = self._direction_opposing(axis, offset_component)
            axis_response = self._calibration.response_for(axis, direction)
            px_per_ms = max(axis_response.px_per_ms, 1e-9)
            duration_ms = max(1, min(cfg.max_pulse_ms, int(abs(offset_component) / px_per_ms)))

            pulse_result = self._mount.pulse_axis(axis, direction, duration_ms)
            pulses += 1
            if not pulse_result.accepted:
                return MountCorrectionResult(False, pulses, dist, "pulse_rejected")

            if cfg.settle_ms > 0:
                time.sleep(cfg.settle_ms / 1000.0)

        final = measure()
        if final.state in _LOCKED_STATES and final.x is not None and final.y is not None:
            final_dist = ((final.x - ref_x) ** 2 + (final.y - ref_y) ** 2) ** 0.5
        else:
            final_dist = 999.0
        success = final_dist <= cfg.rough_tolerance_px
        return MountCorrectionResult(success, pulses, final_dist, "max_pulses")

    def _direction_opposing(self, axis: MountAxis, offset_component: float) -> AxisDirection:
        """Pick the AxisDirection whose calibrated response opposes offset_component."""
        positive_response = self._calibration.response_for(axis, AxisDirection.POSITIVE)
        positive_component = (
            positive_response.dx_px if axis is MountAxis.AXIS1 else positive_response.dy_px
        )
        if (positive_component > 0) != (offset_component > 0):
            return AxisDirection.POSITIVE
        return AxisDirection.NEGATIVE
