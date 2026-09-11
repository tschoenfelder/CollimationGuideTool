"""Shared test doubles and fixtures usable by both apps' test suites.

Synthetic frame factory, replay dataset loader, fake camera/mount.
"""

from astrotool_core.testing.fake_indi_server import FakeIndiServer
from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_mount_park import FakeMountPark
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from astrotool_core.testing.frame_factory import (
    StarSpec,
    bayer_star_field_image,
    donut_image,
    make_frame,
    single_star_image,
    star_field_image,
    with_hot_pixels,
    with_shadow,
)
from astrotool_core.testing.replay_dataset import (
    discover_fits_paths,
    load_expected,
    load_frames,
)
from astrotool_core.testing.shift_grid import (
    CI_SHIFT_GRID_SUBSET,
    KNOWN_SHIFT_GRID,
    ShiftCase,
)

__all__ = [
    "CI_SHIFT_GRID_SUBSET",
    "FakeIndiServer",
    "FakeMountAdapter",
    "FakeMountPark",
    "FakeTouptekCamera",
    "KNOWN_SHIFT_GRID",
    "ShiftCase",
    "StarSpec",
    "bayer_star_field_image",
    "discover_fits_paths",
    "donut_image",
    "load_expected",
    "load_frames",
    "make_frame",
    "single_star_image",
    "star_field_image",
    "with_hot_pixels",
    "with_shadow",
]
