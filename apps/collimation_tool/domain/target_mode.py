"""CollimationTargetMode — issue #39: which kind of point source the
collimation workflow is currently using, so the UI and diagnostics never
leave the operator to infer it.

An artificial star (daytime/cloudy testing) is at a finite distance, so
optical-model quantities that assume a source at infinity (the fine-
collimation diffraction reference/symmetry verdict) are not validated for
it -- `finite_distance` lets callers degrade to an explicit non-actionable
state instead of fabricating a high-confidence result. Nothing here
touches mount tracking or sky coordinates: rough (donut) and fine
collimation are purely image-space already.
"""

from __future__ import annotations

from enum import Enum


class CollimationTargetMode(Enum):
    NATURAL_STAR = "natural_star"
    ARTIFICIAL_STAR = "artificial_star"

    @property
    def label(self) -> str:
        if self is CollimationTargetMode.ARTIFICIAL_STAR:
            return "Artificial star"
        return "Natural star"

    @property
    def finite_distance(self) -> bool:
        return self is CollimationTargetMode.ARTIFICIAL_STAR
