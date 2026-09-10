"""Loader + boundary registry for real-world regression datasets (issue #6).

Parses the ``datasets/regressions/<issue-id>/`` convention documented in
``datasets/regressions/README.md`` and runs each dataset's cases through a
*named public boundary* rather than a per-bug bespoke script, so a reproduced
field bug becomes a permanent, gated regression case.

Imported by the sibling test modules as ``from _loader import ...`` (pytest's
default ``prepend`` import mode puts this directory on ``sys.path`` — same
pattern as ``tests/integration/_golden_master.py``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from astropy.io import fits

#: Repo root — ``tests/regressions/_loader.py`` → parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]
REGRESSIONS_DIR = REPO_ROOT / "datasets" / "regressions"

#: A case input whose path starts with this is a *pointer* to uncommitted data
#: under the git-ignored ``local_test_data/`` tree — resolved relative to the
#: repo root, and treated as "absent → skip" when the file isn't present
#: (issue #6 keeps huge / privacy-sensitive frames out of git while the
#: committed cases still run everywhere).
_LOCAL_POINTER_PREFIX = "local_test_data/"


class RegressionManifestError(ValueError):
    """A ``datasets/regressions/<id>/expected.json`` is missing or malformed."""


@dataclass(frozen=True)
class RegressionCase:
    """One observable expectation within a regression dataset.

    ``rationale`` is required and must state how ``expect`` was independently
    derived (hand measurement, a known software transform, a physical ground
    truth, or "must reject, by inspection") — never copied from the
    implementation under test (issue #6, AC#3).
    """

    name: str
    inputs: dict[str, str]
    expect: dict[str, Any]
    tolerances: dict[str, float]
    rationale: str


@dataclass(frozen=True)
class RegressionManifest:
    """A parsed, validated ``expected.json`` envelope."""

    dataset_dir: Path
    issue: int | None
    diagnostic_uuids: tuple[str, ...]
    boundary: str
    cases: tuple[RegressionCase, ...]


def discover_regression_datasets(root: Path = REGRESSIONS_DIR) -> list[Path]:
    """Every immediate subdirectory of *root* that carries an ``expected.json``."""
    if not root.is_dir():
        return []
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir() and (child / "expected.json").is_file()
    )


def load_manifest(dataset_dir: Path) -> RegressionManifest:
    """Parse and validate ``dataset_dir/expected.json``.

    Raises ``RegressionManifestError`` (with a path-anchored message) for any
    missing/misshaped field so the wiring guard can report it loudly instead
    of the data silently rotting.
    """
    path = dataset_dir / "expected.json"
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:  # pragma: no cover - discover_* guards this
        raise RegressionManifestError(f"{path}: no expected.json") from exc
    except json.JSONDecodeError as exc:
        raise RegressionManifestError(f"{path}: invalid JSON — {exc}") from exc
    if not isinstance(raw, dict):
        raise RegressionManifestError(f"{path}: top level must be a JSON object")

    issue = _validate_origin(path, raw)
    boundary = raw.get("boundary")
    if not isinstance(boundary, str) or not boundary:
        raise RegressionManifestError(f"{path}: 'boundary' must be a non-empty string")
    cases = _parse_cases(path, raw.get("cases"))
    uuids = tuple(str(item) for item in raw.get("diagnostic_uuids", []) or [])
    return RegressionManifest(
        dataset_dir=dataset_dir,
        issue=issue,
        diagnostic_uuids=uuids,
        boundary=boundary,
        cases=cases,
    )


def resolve_input(dataset_dir: Path, ref: str) -> np.ndarray | None:
    """Load one case-input's pixels as a ``float32`` array.

    Returns ``None`` when *ref* is a ``local_test_data/`` pointer whose target
    file is absent, so the caller can skip that case on a checkout without the
    uncommitted data.
    """
    if ref.startswith(_LOCAL_POINTER_PREFIX):
        target = REPO_ROOT / ref
        if not target.is_file():
            return None
    else:
        target = dataset_dir / ref
    return np.asarray(fits.getdata(target), dtype=np.float32)


# --- boundary registry ------------------------------------------------------

#: Runs one case: ``{role: pixels}`` plus that case's own ``expect`` dict (a few
#: boundaries need a parameter from it) → a flat dict comparable against
#: ``expect``. Downstream issues (#28/#29/#30) append their own entries.
RegressionBoundaryRunner = Callable[[dict[str, np.ndarray], dict[str, Any]], dict[str, Any]]


def _run_measure_translation_offset(
    inputs: dict[str, np.ndarray], _expect: dict[str, Any]
) -> dict[str, Any]:
    from astrotool_core.target.translation_offset import measure_translation_offset

    result = measure_translation_offset(inputs["before"], inputs["after"])
    if result is None:
        return {"match": False, "dx_px": None, "dy_px": None, "score": None}
    return {
        "match": True,
        "dx_px": result.dx_px,
        "dy_px": result.dy_px,
        "score": result.score,
    }


BOUNDARIES: dict[str, RegressionBoundaryRunner] = {
    "measure_translation_offset": _run_measure_translation_offset,
}


def run_case(manifest: RegressionManifest, case: RegressionCase) -> dict[str, Any] | None:
    """Resolve *case*'s inputs and run *manifest*'s boundary.

    Returns ``None`` when any input is an absent ``local_test_data/`` pointer.
    """
    runner = BOUNDARIES[manifest.boundary]
    resolved: dict[str, np.ndarray] = {}
    for role, ref in case.inputs.items():
        pixels = resolve_input(manifest.dataset_dir, ref)
        if pixels is None:
            return None
        resolved[role] = pixels
    return runner(resolved, case.expect)


def assert_case_expectation(actual: dict[str, Any], case: RegressionCase) -> None:
    """Assert *actual* satisfies ``case.expect``.

    Keys named in ``case.tolerances`` are compared with
    ``pytest.approx(want, abs=tol)``; every other ``expect`` key must match
    exactly — so ``{"match": false}`` and string / ``None`` expectations work
    too. The single comparison implementation every regression dataset (generic
    driver or bespoke test) shares.
    """
    for key, want in case.expect.items():
        got = actual.get(key)
        tol = case.tolerances.get(key)
        if tol is not None:
            assert got == pytest.approx(want, abs=tol), (
                f"{case.name}: {key} = {got!r} drifted beyond +/-{tol} of {want!r}"
            )
        else:
            assert got == want, f"{case.name}: {key} = {got!r}, expected {want!r}"


# --- manifest validation helpers -----------------------------------------------


def _validate_origin(path: Path, raw: dict[str, Any]) -> int | None:
    issue = raw.get("issue")
    uuids = raw.get("diagnostic_uuids")
    has_issue = isinstance(issue, int) and not isinstance(issue, bool)
    has_uuids = isinstance(uuids, list) and len(uuids) > 0
    if not has_issue and not has_uuids:
        raise RegressionManifestError(
            f"{path}: needs an integer 'issue' or a non-empty 'diagnostic_uuids' list "
            "(issue #6, AC#2 — the dataset must name its origin)"
        )
    return issue if has_issue else None


def _parse_cases(path: Path, raw_cases: object) -> tuple[RegressionCase, ...]:
    if not isinstance(raw_cases, list) or not raw_cases:
        raise RegressionManifestError(f"{path}: 'cases' must be a non-empty list")
    cases: list[RegressionCase] = []
    seen: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        case = _parse_one_case(f"{path}: cases[{index}]", raw_case)
        if case.name in seen:
            raise RegressionManifestError(f"{path}: duplicate case name {case.name!r}")
        seen.add(case.name)
        cases.append(case)
    return tuple(cases)


def _parse_one_case(where: str, raw_case: object) -> RegressionCase:
    if not isinstance(raw_case, dict):
        raise RegressionManifestError(f"{where}: must be an object")
    name = _require_nonempty_str(raw_case.get("name"), f"{where}.name")
    rationale = raw_case.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        raise RegressionManifestError(
            f"{where} ({name}).rationale: must be non-empty — cite how the expected value "
            "was independently derived, never copied from the implementation (issue #6, AC#3)"
        )
    return RegressionCase(
        name=name,
        inputs=_require_str_str_map(raw_case.get("inputs"), f"{where} ({name}).inputs"),
        expect=_require_nonempty_obj(raw_case.get("expect"), f"{where} ({name}).expect"),
        tolerances=_require_number_map(raw_case.get("tolerances"), f"{where} ({name}).tolerances"),
        rationale=rationale,
    )


def _require_nonempty_str(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise RegressionManifestError(f"{where}: must be a non-empty string")
    return value


def _require_nonempty_obj(value: object, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise RegressionManifestError(f"{where}: must be a non-empty object")
    return dict(value)


def _require_str_str_map(value: object, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise RegressionManifestError(f"{where}: must be a non-empty object")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise RegressionManifestError(f"{where}: every key and value must be a string")
        out[key] = item
    return out


def _require_number_map(value: object, where: str) -> dict[str, float]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RegressionManifestError(f"{where}: must be an object of numbers")
    out: dict[str, float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool) or not isinstance(item, (int, float)):
            raise RegressionManifestError(f"{where}: every value must be a number")
        out[key] = float(item)
    return out
