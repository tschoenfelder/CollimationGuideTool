"""Every proof manifest in proofs/ is valid (issue #54): schema, production modules and
test files exist, each listed node's classes/function exist, the focused command runs every
listed regression test, and the fix commits are in git (a shallow CI clone only warns).

`python scripts/prove.py --validate-all` (CI fast job) additionally collects the listed node
ids with pytest; the tests themselves run in their own tiers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prove.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prove_contract", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prove = _load_script()
_MANIFESTS = prove.load_manifests()


def test_the_backfilled_manifests_are_present() -> None:
    """Issue #54's closure proof: >= 5 backfilled manifests, #43 and #49 among them."""
    assert len(_MANIFESTS) >= 5
    covered = {issue for manifest in _MANIFESTS for issue in manifest.issues}
    assert {43, 49} <= covered


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=lambda m: m.proof_id)
def test_manifest_is_valid(manifest: object) -> None:
    assert prove.static_problems(manifest) == []


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=lambda m: m.proof_id)
def test_fix_commits_exist(manifest: object) -> None:
    errors, _shallow_clone_warnings = prove.commit_problems(manifest)
    assert errors == []


def test_at_least_three_manifests_carry_revert_proof_evidence() -> None:
    proven = [
        m.proof_id
        for m in _MANIFESTS
        if any(t.get("failed_before_fix") for t in m.data.get("regression_tests", []))
    ]
    assert len(proven) >= 3, proven
