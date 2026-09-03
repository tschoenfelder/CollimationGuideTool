"""OpticalPrior — the persistent half of issue #29's "OPTICAL PRIOR vs
CURRENT REGISTRATION" split: sensor geometry and plate scale for one
optical train, known from configuration before any image matching runs
and mostly stable until the hardware itself changes (a different camera,
a refocuser, a reducer swap). Deliberately holds nothing measured from a
frame (rotation, boresight offset, overlap) — that's
`registration.result.CrossCameraRegistrationResult`'s job, re-measurable
on every registration attempt, never assumed to still match a prior run.

A plain, free-form `name` (not a `Main`/`Guide` enum) so the registration
core never hard-codes which optical trains exist or how many there are —
see this package's own docstring and issue #29's "Core registration logic
is independent of hard-coded Main/Guide camera names and camera count"
acceptance criterion.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OpticalPrior:
    """One optical train's known geometry. `pixel_scale_arcsec` is
    arcsec/px — see `astrotool_core.optics.smarttscope_config
    .load_pixel_scale_arcsec` for where this project's own real value
    comes from today."""

    name: str
    sensor_width_px: int
    sensor_height_px: int
    pixel_scale_arcsec: float

    def __post_init__(self) -> None:
        if self.sensor_width_px <= 0 or self.sensor_height_px <= 0:
            raise ValueError("OpticalPrior: sensor dimensions must be positive")
        if self.pixel_scale_arcsec <= 0:
            raise ValueError("OpticalPrior: pixel_scale_arcsec must be positive")

    @property
    def fov_width_arcsec(self) -> float:
        return self.pixel_scale_arcsec * self.sensor_width_px

    @property
    def fov_height_arcsec(self) -> float:
        return self.pixel_scale_arcsec * self.sensor_height_px


def scale_ratio(source: OpticalPrior, target: OpticalPrior) -> float:
    """`target`-pixels per one `source`-pixel — the starting-point scale
    estimate for locating `source`'s frame content within `target`'s
    (e.g. terrestrial registration's own `approx_scale`), derived purely
    from each train's own known plate scale rather than searched blindly
    (issue #29: "scale is mostly fixed... should be derived from
    configuration rather than searched blindly"). The two trains' actual
    relative *rotation* and *boresight offset* are NOT derivable from
    this alone — those are exactly what registration measures."""
    return source.pixel_scale_arcsec / target.pixel_scale_arcsec
