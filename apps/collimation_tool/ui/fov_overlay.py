"""Computes where the main camera's field of view falls within the guide
camera's field of view — a normalized rectangle, independent of either
camera's actual pixel resolution, so it applies cleanly to whatever size
`LiveViewLabel` is currently displaying the guide frame at.

Plate scale for each optical train comes from
`astrotool_core.optics.smarttscope_config` (read once at app startup —
see MainWindow's docstring). No pointing-offset or rotation data exists
anywhere in that config (searched for it — see issue tracker); the
rectangle is centered and unrotated, deliberately labeled as a
placeholder pending real guide-to-main alignment calibration rather than
presented as measured.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FovOverlayRect:
    """A rectangle in the guide frame, normalized to [0, 1] on each axis
    (x/y are the top-left corner) — apply by multiplying by whatever
    pixmap size the guide view is currently rendered at."""

    x: float
    y: float
    width: float
    height: float


def compute_fov_overlay_rect(
    *,
    main_pixel_scale_arcsec: float,
    main_sensor_width_px: int,
    main_sensor_height_px: int,
    guide_pixel_scale_arcsec: float,
    guide_sensor_width_px: int,
    guide_sensor_height_px: int,
) -> FovOverlayRect | None:
    """The main camera's FOV as a centered rectangle within the guide FOV.

    Returns None for any non-positive/missing input — the caller (an
    optical train's plate scale being unavailable, e.g. no SmartTScope
    config found) should draw no overlay at all rather than a
    meaningless one.
    """
    if (
        main_pixel_scale_arcsec <= 0
        or guide_pixel_scale_arcsec <= 0
        or main_sensor_width_px <= 0
        or main_sensor_height_px <= 0
        or guide_sensor_width_px <= 0
        or guide_sensor_height_px <= 0
    ):
        return None

    main_fov_width_arcsec = main_pixel_scale_arcsec * main_sensor_width_px
    main_fov_height_arcsec = main_pixel_scale_arcsec * main_sensor_height_px
    guide_fov_width_arcsec = guide_pixel_scale_arcsec * guide_sensor_width_px
    guide_fov_height_arcsec = guide_pixel_scale_arcsec * guide_sensor_height_px

    width_fraction = main_fov_width_arcsec / guide_fov_width_arcsec
    height_fraction = main_fov_height_arcsec / guide_fov_height_arcsec

    # An unusual setup (or a config mismatch) could put the main FOV
    # partly or fully outside the guide FOV — clip to the guide frame
    # rather than return an off-frame or larger-than-1 rectangle.
    width_fraction = min(1.0, width_fraction)
    height_fraction = min(1.0, height_fraction)

    x = (1.0 - width_fraction) / 2.0
    y = (1.0 - height_fraction) / 2.0
    return FovOverlayRect(x=x, y=y, width=width_fraction, height=height_fraction)
