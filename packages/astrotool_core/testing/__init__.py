"""Shared test doubles and fixtures usable by both apps' test suites.

Synthetic frame factory, replay dataset loader, fake camera/mount.
"""

from astrotool_core.testing.fake_mount import FakeMountAdapter
from astrotool_core.testing.fake_touptek import FakeTouptekCamera
from astrotool_core.testing.frame_factory import (
    StarSpec,
    bayer_star_field_image,
    make_frame,
    single_star_image,
    star_field_image,
    with_hot_pixels,
)

__all__ = [
    "FakeMountAdapter",
    "FakeTouptekCamera",
    "StarSpec",
    "bayer_star_field_image",
    "make_frame",
    "single_star_image",
    "star_field_image",
    "with_hot_pixels",
]
