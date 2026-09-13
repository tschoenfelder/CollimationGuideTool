"""Radial diffraction-profile analysis of focused, stacked star images.

Consumes Stage 3's (`astrotool_core.target.stacking`) stacked output.
Home for Stage 4 (issue #18) and, later, Stages 5-6's own diffraction
analysis -- deliberately not part of `astrotool_core.target`, whose own
docstring scopes it to point-source detection and ROI tracking.
"""

from astrotool_core.diffraction.optical_reference_model import (
    DiffractionReferenceResult,
    OpticalConfig,
    compute_diffraction_reference,
)
from astrotool_core.diffraction.radial_profile import (
    RadialProfile,
    RadialProfileResult,
    compute_radial_profile,
)

__all__ = [
    "DiffractionReferenceResult",
    "OpticalConfig",
    "RadialProfile",
    "RadialProfileResult",
    "compute_diffraction_reference",
    "compute_radial_profile",
]
