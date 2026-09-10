"""Generic driver for every declarative real-world regression dataset.

Walks ``datasets/regressions/*/``, and for each case runs the dataset's named
boundary and asserts its ``expect`` (issue #6, AC#4 — one reusable loader
instead of a bespoke script per bug). A dataset whose ``boundary`` isn't
registered, or whose ``expected.json`` is malformed, is *not* silently ignored
here — ``test_every_regression_dataset_is_wired.py`` fails loudly on it.

Cases whose inputs point at absent ``local_test_data/`` frames skip cleanly.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _loader import (
    BOUNDARIES,
    RegressionManifestError,
    assert_case_expectation,
    discover_regression_datasets,
    load_manifest,
    run_case,
)


def _collect_cases() -> list[tuple[Path, str]]:
    collected: list[tuple[Path, str]] = []
    for dataset_dir in discover_regression_datasets():
        try:
            manifest = load_manifest(dataset_dir)
        except RegressionManifestError:
            continue  # reported by the wiring guard, not here
        if manifest.boundary not in BOUNDARIES:
            continue  # ditto
        for case in manifest.cases:
            collected.append((dataset_dir, case.name))
    return collected


_CASES = _collect_cases()


@pytest.mark.parametrize(
    ("dataset_dir", "case_name"),
    _CASES,
    ids=[f"{dataset_dir.name}-{case_name}" for dataset_dir, case_name in _CASES],
)
def test_regression_case(dataset_dir: Path, case_name: str) -> None:
    manifest = load_manifest(dataset_dir)
    case = next(candidate for candidate in manifest.cases if candidate.name == case_name)

    actual = run_case(manifest, case)
    if actual is None:
        pytest.skip(
            f"{dataset_dir.name}/{case_name}: a local_test_data/ pointer input is not "
            "present on this checkout"
        )

    assert_case_expectation(actual, case)
