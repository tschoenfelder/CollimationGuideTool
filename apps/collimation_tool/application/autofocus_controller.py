"""AutofocusController — issue #33's shared workflow: computes the hard
safety envelope and drives BoundedFocusSearcher (`autofocus_search.py`)
with whichever mode-specific analyzer (star or terrestrial) was selected,
while enforcing frame-freshness and a stable exposure/gain for the whole
run.

Same constructor-injected-callables DI shape `MountTestMovePanel` already
established for its own camera access (`get_left_frame`,
`wait_for_left_frame` == `CameraPanel.wait_for_frame_after`,
`set_left_auto_exposure_paused` == `CameraPanel.set_auto_exposure_paused`)
-- this controller takes exactly that shape of callables for whichever
camera panel/focuser pairing it's attached to, so it never depends on
`CameraPanel`/Qt directly and stays independently testable.

Frame-freshness: `BoundedFocusSearcher` itself already waits for the
focuser to report movement complete before ever calling this controller's
own `measure` closure (see `autofocus_search.BoundedFocusSearcher.
_wait_for_move_settled`) -- so by the time `measure()` runs here, the
physical move is already known complete, and passing `time.monotonic()`
(captured right then) as `wait_for_frame`'s own `reference_monotonic` is
guaranteed to reject any frame whose exposure started before that.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from astrotool_core.acquisition.stable_frame_acquisition import FrameAcquisitionResult
from astrotool_core.focus.port import FocuserPort
from astrotool_core.focus.search_bounds import DEFAULT_ENVELOPE_STEPS
from astrotool_core.focus.star_focus_metric import measure_star_focus
from astrotool_core.focus.terrestrial_focus_metric import measure_terrestrial_focus

from collimation_tool.application.autofocus_search import (
    AutofocusStatus,
    BoundedFocusSearcher,
    FocusCurvePoint,
    FocusSample,
)

GetFrame = Callable[[], "np.ndarray | None"]
WaitForFrame = Callable[[float, float], FrameAcquisitionResult]
SetAutoExposurePaused = Callable[[bool], None]


class AutofocusMode(Enum):
    STAR = "star"
    TERRESTRIAL = "terrestrial"


@dataclass(frozen=True)
class AutofocusResult:
    status: AutofocusStatus
    mode: AutofocusMode
    start_position: int
    best_position: int | None
    search_min: int | None
    search_max: int | None
    samples: tuple[FocusCurvePoint, ...] = field(default_factory=tuple)
    confidence: float = 0.0
    final_value: float | None = None
    #: Issue #35: which camera/optical-train/focuser this run actually
    #: used. Defaulted to "unknown" (not required) so existing callers
    #: are unaffected; this app has no independent per-train focuser
    #: yet, so camera_label/optical_train/focuser_label are today always
    #: the same single value, passed in by whichever panel owns this
    #: controller -- not three independently-tracked identities.
    camera_label: str = "unknown"
    optical_train: str = "unknown"
    focuser_label: str = "unknown"


class AutofocusController:
    def __init__(
        self,
        focuser: FocuserPort,
        *,
        get_frame: GetFrame,
        wait_for_frame: WaitForFrame,
        set_auto_exposure_paused: SetAutoExposurePaused,
        frame_timeout_s: float = 5.0,
        terrestrial_sample_count: int = 3,
        coarse_step: int = 250,
        fine_step: int = 25,
        max_coarse_steps: int = 20,
        max_consecutive_no_improve: int = 2,
        improvement_fraction: float = 0.05,
        final_approach_direction: int = 1,
        envelope_steps: int = DEFAULT_ENVELOPE_STEPS,
        star_edge_margin_px: int = 16,
        terrestrial_tile_size_px: int = 64,
        camera_label: str = "unknown",
        focuser_label: str = "unknown",
    ) -> None:
        self._focuser = focuser
        self._get_frame = get_frame
        self._wait_for_frame = wait_for_frame
        self._set_auto_exposure_paused = set_auto_exposure_paused
        self._camera_label = camera_label
        self._focuser_label = focuser_label
        self._frame_timeout_s = frame_timeout_s
        self._terrestrial_sample_count = max(1, terrestrial_sample_count)
        self._star_edge_margin_px = star_edge_margin_px
        self._terrestrial_tile_size_px = terrestrial_tile_size_px
        self._coarse_step = coarse_step
        self._fine_step = fine_step
        self._max_coarse_steps = max_coarse_steps
        self._max_consecutive_no_improve = max_consecutive_no_improve
        self._improvement_fraction = improvement_fraction
        self._final_approach_direction = final_approach_direction
        self._envelope_steps = envelope_steps

    def run(
        self, mode: AutofocusMode, cancel_check: Callable[[], bool] | None = None
    ) -> AutofocusResult:
        self._set_auto_exposure_paused(True)
        try:
            searcher = BoundedFocusSearcher(
                self._focuser, higher_is_better=(mode is AutofocusMode.TERRESTRIAL),
                coarse_step=self._coarse_step, fine_step=self._fine_step,
                max_coarse_steps=self._max_coarse_steps,
                max_consecutive_no_improve=self._max_consecutive_no_improve,
                improvement_fraction=self._improvement_fraction,
                final_approach_direction=self._final_approach_direction,
                envelope_steps=self._envelope_steps,
            )
            measure = self._build_measurer(mode)
            search_result = searcher.search(measure, cancel_check=cancel_check)
        finally:
            self._set_auto_exposure_paused(False)

        confidence = search_result.samples[-1].confidence if search_result.samples else 0.0
        return AutofocusResult(
            status=search_result.status,
            mode=mode,
            start_position=search_result.start_position,
            best_position=search_result.best_position,
            search_min=search_result.search_min,
            search_max=search_result.search_max,
            samples=search_result.samples,
            confidence=confidence,
            final_value=search_result.final_value,
            camera_label=self._camera_label,
            optical_train=self._camera_label,
            focuser_label=self._focuser_label,
        )

    def _build_measurer(self, mode: AutofocusMode) -> Callable[[], FocusSample | None]:
        if mode is AutofocusMode.STAR:
            return self._measure_star
        return self._measure_terrestrial

    def _acquire_fresh_frame(self) -> np.ndarray | None:
        reference_monotonic = time.monotonic()
        acquisition = self._wait_for_frame(reference_monotonic, self._frame_timeout_s)
        if not acquisition.ok or acquisition.frame is None:
            return None
        return acquisition.frame.pixels

    def _measure_star(self) -> FocusSample | None:
        frame = self._acquire_fresh_frame()
        if frame is None:
            return None
        measurement = measure_star_focus(frame, edge_margin_px=self._star_edge_margin_px)
        if measurement.fwhm_px is None:
            return None
        return FocusSample(value=measurement.fwhm_px, confidence=measurement.confidence)

    def _measure_terrestrial(self) -> FocusSample | None:
        first = self._acquire_fresh_frame()
        if first is None:
            return None
        frames: list[np.ndarray] = [first]
        for _ in range(self._terrestrial_sample_count - 1):
            extra = self._get_frame()
            if extra is not None:
                frames.append(extra)
        measurement = measure_terrestrial_focus(frames, tile_size_px=self._terrestrial_tile_size_px)
        if measurement.sharpness is None:
            return None
        return FocusSample(value=measurement.sharpness, confidence=measurement.confidence)
