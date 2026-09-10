"""Loud guard against dead regression data (issue #6, AC#5).

Every ``datasets/regressions/<id>/`` directory must (a) carry a valid
``expected.json`` and (b) actually be exercised by a test — either its
``boundary`` is registered (the generic driver in
``test_regression_datasets.py`` runs it) or a bespoke
``tests/regressions/test_*.py`` module names the dataset directory. A dataset
meeting neither condition fails here rather than silently rotting.

Mirrors ``tests/integration/test_placeholder_datasets_skip_cleanly.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _loader import (
    BOUNDARIES,
    REGRESSIONS_DIR,
    RegressionManifestError,
    discover_regression_datasets,
    load_manifest,
)

_TESTS_DIR = Path(__file__).resolve().parent
_GUARD_MODULES = {"test_regression_datasets.py", "test_every_regression_dataset_is_wired.py"}

_DATASETS = discover_regression_datasets()


def _bespoke_modules_naming(dataset_name: str) -> list[str]:
    hits: list[str] = []
    for module in sorted(_TESTS_DIR.glob("test_*.py")):
        if module.name in _GUARD_MODULES:
            continue
        if dataset_name in module.read_text(encoding="utf-8"):
            hits.append(module.name)
    return hits


def test_example_template_dataset_exists() -> None:
    """AC#8: the copy-me synthetic template must always be present."""
    assert (REGRESSIONS_DIR / "example" / "expected.json").is_file(), (
        "datasets/regressions/example/ is issue #6's required template case — "
        "it must not be deleted."
    )


@pytest.mark.parametrize("dataset_dir", _DATASETS, ids=[dataset.name for dataset in _DATASETS])
def test_regression_dataset_is_valid_and_wired(dataset_dir: Path) -> None:
    try:
        manifest = load_manifest(dataset_dir)
    except RegressionManifestError as exc:
        pytest.fail(f"{dataset_dir.name}: invalid expected.json — {exc}")

    covered_by_driver = manifest.boundary in BOUNDARIES
    covered_by_bespoke = bool(_bespoke_modules_naming(dataset_dir.name))
    if not covered_by_driver and not covered_by_bespoke:
        pytest.fail(
            f"{dataset_dir.name}: boundary {manifest.boundary!r} has no registered runner in "
            "_loader.BOUNDARIES and no bespoke tests/regressions/test_*.py names this dataset — "
            "wire it or the data is dead (issue #6, AC#5)."
        )
