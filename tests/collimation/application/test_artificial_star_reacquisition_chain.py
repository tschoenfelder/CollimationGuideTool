"""Issue #37 -> #39 contract: a registration produced by the REAL
ArtificialStarRegistrar drives the REAL guide-assisted reacquisition in
FocusedStarAcquisition, inside the fine-collimation loop -- no ASTAP, no
mount tracking, no celestial coordinates anywhere in the chain."""

from __future__ import annotations

import numpy as np
from astrotool_core.diffraction.optical_reference_model import OpticalConfig
from astrotool_core.mount.axis_calibration import AxisResponse, CalibrationMatrix
from astrotool_core.mount.port import (
    AxisDirection,
    CommandResult,
    MountAxis,
    MountCapabilities,
    MountStatus,
)
from astrotool_core.registration.artificial_star_registrar import ArtificialStarRegistrar
from astrotool_core.registration.optical_prior import OpticalPrior
from astrotool_core.testing.frame_factory import airy_pattern_image, single_star_image
from collimation_tool.application.fine_collimation_controller import FineCollimationController
from collimation_tool.application.star_acquisition import (
    AcquisitionResult,
    FocusedStarAcquisition,
)
from collimation_tool.domain.target_mode import CollimationTargetMode

_MAIN_SHAPE = (140, 140)
_GUIDE_SHAPE = (400, 400)
_VECTORS = {
    (MountAxis.AXIS1, AxisDirection.POSITIVE): (10.0, 0.0),
    (MountAxis.AXIS1, AxisDirection.NEGATIVE): (-10.0, 0.0),
    (MountAxis.AXIS2, AxisDirection.POSITIVE): (0.0, 10.0),
    (MountAxis.AXIS2, AxisDirection.NEGATIVE): (0.0, -10.0),
}


def _calibration() -> CalibrationMatrix:
    return CalibrationMatrix(
        responses={
            key: AxisResponse(
                axis=key[0], direction=key[1], duration_ms=100, dx_px=dx, dy_px=dy, px_per_ms=0.1
            )
            for key, (dx, dy) in _VECTORS.items()
        }
    )


class _GuideMount:
    def __init__(self, calibration: CalibrationMatrix, start: tuple[float, float]) -> None:
        self._calibration = calibration
        self.x, self.y = start
        self.pulses = 0

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def capabilities(self) -> MountCapabilities:
        return MountCapabilities(supports_pulse_guiding=True, min_pulse_ms=1, max_pulse_ms=9999)

    def status(self) -> MountStatus:
        return MountStatus(connected=True, tracking=False, slewing=False)  # tracking OFF

    def pulse_axis(
        self, axis: MountAxis, direction: AxisDirection, duration_ms: int,
        *, rate_preset: str | None = None,
    ) -> CommandResult:
        self.pulses += 1
        response = self._calibration.response_for(axis, direction)
        self.x += response.dx_px / response.duration_ms * duration_ms
        self.y += response.dy_px / response.duration_ms * duration_ms
        return CommandResult(accepted=True)

    def render_frame(self) -> np.ndarray:
        return single_star_image(
            _GUIDE_SHAPE, x=self.x, y=self.y, peak=3000.0, sigma=2.0, background=100.0
        )


def _main_star(x: float) -> np.ndarray:
    return airy_pattern_image(
        _MAIN_SHAPE, x=x, y=70.0, peak=5000.0, core_sigma=2.0, background=100.0,
        ring_radius_px=12.0, ring_peak=1200.0, ring_sigma=1.5,
    )


def _blank() -> np.ndarray:
    return np.full(_MAIN_SHAPE, 100.0) + np.random.default_rng(0).normal(0.0, 5.0, _MAIN_SHAPE)


def test_a_37_registration_drives_guide_reacquisition_inside_the_fine_loop() -> None:
    prior_main = OpticalPrior(
        name="main", sensor_width_px=140, sensor_height_px=140, pixel_scale_arcsec=1.0
    )
    prior_guide = OpticalPrior(
        name="guide", sensor_width_px=400, sensor_height_px=400, pixel_scale_arcsec=1.0
    )
    # The artificial star sits right of Main's center; Guide sees it at (240, 200).
    registration = ArtificialStarRegistrar().register(
        _main_star(100.0),
        single_star_image(_GUIDE_SHAPE, x=240.0, y=200.0, peak=3000.0, sigma=2.0),
        prior_main,
        prior_guide,
    )
    assert registration.ok

    calibration = _calibration()
    mount = _GuideMount(calibration, start=(240.0, 200.0))

    def reacquirer(acq: FocusedStarAcquisition, cancel: object) -> AcquisitionResult:
        return acq.attempt_guide_reacquisition(
            mount.render_frame, mount=mount, guide_calibration=calibration,
            registration=registration, prior_main=prior_main,
        )

    # Star tracked near Main's right side, lost for two frames, then back
    # near Main's center once the guide-assisted recentering brings it home.
    frames = iter([_main_star(100.0), _blank(), _blank(), _main_star(70.0)])
    controller = FineCollimationController(
        FocusedStarAcquisition(roi_size=(120, 120), max_full_frame_search_attempts=2),
        get_frame=lambda: next(frames, _main_star(70.0)),
        optical_config=OpticalConfig(),
        sample_count=4,
        target_mode=CollimationTargetMode.ARTIFICIAL_STAR,
        guide_reacquirer=reacquirer,
    )

    outcome = controller.run()

    assert outcome.status == "success"
    assert mount.pulses > 0  # the real recenter policy actually moved the mount
    assert "reacquired_via_guide" in outcome.reacquisition_log
    assert outcome.result is not None
    assert outcome.result.target_mode is CollimationTargetMode.ARTIFICIAL_STAR
