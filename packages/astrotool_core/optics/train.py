"""Optical-train identifiers (#55 D05, S6.9): the ONE owner of the train names.

A train is named "Main"/"Guide" in filter-wheel assignments (`used_by`) and "main"/"guide" in
config roles (SmartTScope's `active_camera_role`); matching is case-insensitive, so both
spellings name the same train. `train_key` is that matching rule.
"""

from __future__ import annotations

MAIN_TRAIN = "Main"
GUIDE_TRAIN = "Guide"


def train_key(name: str) -> str:
    """The case-insensitive key of a train name ("Main" and "main" are the same train)."""
    return name.lower()
