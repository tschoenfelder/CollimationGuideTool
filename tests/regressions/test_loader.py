"""Unit tests for ``_loader`` — the regression-dataset manifest parser,
input resolver, and boundary registry (issue #6).

Built entirely against ``tmp_path`` fixtures, mirroring
``tests/core/testing/test_replay_dataset.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from _loader import (
    BOUNDARIES,
    RegressionCase,
    RegressionManifestError,
    assert_case_expectation,
    discover_regression_datasets,
    load_manifest,
    resolve_input,
    run_case,
)
from astropy.io import fits

_VALID: dict[str, object] = {
    "issue": 42,
    "diagnostic_uuids": ["abcd1234-0000-0000-0000-000000000000"],
    "git_commit_at_capture": "deadbeefcafe",
    "user_report": "it broke",
    "optical_setup": "Main mono 0.4\"/px",
    "boundary": "measure_translation_offset",
    "cases": [
        {
            "name": "case_a",
            "inputs": {"before": "frames/a.fits", "after": "frames/b.fits"},
            "expect": {"match": True, "dx_px": 5.0},
            "tolerances": {"dx_px": 1.0},
            "rationale": "shift applied by hand",
        }
    ],
}


def _write_manifest(dataset_dir: Path, payload: object) -> Path:
    dataset_dir.mkdir(parents=True, exist_ok=True)
    (dataset_dir / "expected.json").write_text(json.dumps(payload), encoding="utf-8")
    return dataset_dir


# --- discover_regression_datasets --------------------------------------------


def test_discover_returns_only_dirs_with_an_expected_json(tmp_path: Path) -> None:
    _write_manifest(tmp_path / "34", _VALID)
    (tmp_path / "not_a_dataset").mkdir()
    (tmp_path / "loose_file.txt").write_text("x", encoding="utf-8")

    found = discover_regression_datasets(tmp_path)

    assert [p.name for p in found] == ["34"]


def test_discover_returns_empty_for_a_missing_root(tmp_path: Path) -> None:
    assert discover_regression_datasets(tmp_path / "nope") == []


# --- load_manifest: happy path ---------------------------------------------------


def test_load_manifest_parses_a_valid_envelope(tmp_path: Path) -> None:
    dataset_dir = _write_manifest(tmp_path / "42", _VALID)

    manifest = load_manifest(dataset_dir)

    assert manifest.issue == 42
    assert manifest.diagnostic_uuids == ("abcd1234-0000-0000-0000-000000000000",)
    assert manifest.boundary == "measure_translation_offset"
    assert len(manifest.cases) == 1
    case = manifest.cases[0]
    assert case.name == "case_a"
    assert case.inputs == {"before": "frames/a.fits", "after": "frames/b.fits"}
    assert case.tolerances == {"dx_px": 1.0}
    assert case.rationale == "shift applied by hand"


def test_uuid_only_origin_is_accepted(tmp_path: Path) -> None:
    payload = {**_VALID}
    del payload["issue"]
    dataset_dir = _write_manifest(tmp_path / "diag-abcd1234", payload)

    manifest = load_manifest(dataset_dir)

    assert manifest.issue is None
    assert manifest.diagnostic_uuids == ("abcd1234-0000-0000-0000-000000000000",)


# --- load_manifest: rejections ------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda p: p.update(issue=None) or p.update(diagnostic_uuids=[]), "origin"),
        (lambda p: p.pop("boundary"), "boundary"),
        (lambda p: p.update(boundary=""), "boundary"),
        (lambda p: p.update(cases=[]), "cases"),
        (lambda p: p["cases"][0].pop("name"), "name"),
        (lambda p: p["cases"][0].pop("inputs"), "inputs"),
        (lambda p: p["cases"][0].update(inputs={"before": 3}), "string"),
        (lambda p: p["cases"][0].pop("expect"), "expect"),
        (lambda p: p["cases"][0].pop("rationale"), "rationale"),
        (lambda p: p["cases"][0].update(rationale="   "), "rationale"),
        (lambda p: p["cases"][0].update(tolerances={"dx_px": "loads"}), "number"),
    ],
)
def test_load_manifest_rejects_a_malformed_envelope(
    tmp_path: Path, mutate: object, needle: str
) -> None:
    payload = json.loads(json.dumps(_VALID))  # deep copy
    mutate(payload)  # type: ignore[operator]
    dataset_dir = _write_manifest(tmp_path / "bad", payload)

    with pytest.raises(RegressionManifestError, match=needle):
        load_manifest(dataset_dir)


def test_load_manifest_rejects_duplicate_case_names(tmp_path: Path) -> None:
    payload = json.loads(json.dumps(_VALID))
    payload["cases"].append(json.loads(json.dumps(payload["cases"][0])))
    dataset_dir = _write_manifest(tmp_path / "dup", payload)

    with pytest.raises(RegressionManifestError, match="duplicate case name"):
        load_manifest(dataset_dir)


def test_load_manifest_rejects_invalid_json(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "broken"
    dataset_dir.mkdir()
    (dataset_dir / "expected.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(RegressionManifestError, match="invalid JSON"):
        load_manifest(dataset_dir)


# --- resolve_input -----------------------------------------------------------


def _write_fits(path: Path, value: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data=np.full((4, 4), value, dtype=np.float32)).writeto(path, overwrite=True)


def test_resolve_input_loads_a_committed_frame(tmp_path: Path) -> None:
    _write_fits(tmp_path / "frames" / "a.fits", 7.0)

    pixels = resolve_input(tmp_path, "frames/a.fits")

    assert pixels is not None
    assert pixels.dtype == np.float32
    assert pixels.shape == (4, 4)
    assert float(pixels[0, 0]) == 7.0


def test_resolve_input_returns_none_for_an_absent_local_pointer(tmp_path: Path) -> None:
    result = resolve_input(tmp_path, "local_test_data/nope/frames/x.fits")

    assert result is None


# --- boundary registry + run_case ------------------------------------------------


def test_measure_translation_offset_boundary_is_registered() -> None:
    assert "measure_translation_offset" in BOUNDARIES


def test_run_case_executes_the_translation_offset_boundary(tmp_path: Path) -> None:
    rng = np.random.default_rng(1)
    base = rng.normal(500.0, 80.0, size=(48, 48)).astype(np.float32)
    (tmp_path / "frames").mkdir()
    fits.PrimaryHDU(data=base).writeto(tmp_path / "frames" / "b.fits", overwrite=True)
    fits.PrimaryHDU(data=np.roll(base, shift=(0, 4), axis=(0, 1))).writeto(
        tmp_path / "frames" / "a.fits", overwrite=True
    )
    payload = json.loads(json.dumps(_VALID))
    payload["cases"][0]["inputs"] = {"before": "frames/b.fits", "after": "frames/a.fits"}
    manifest = load_manifest(_write_manifest(tmp_path, payload))

    actual = run_case(manifest, manifest.cases[0])

    assert actual is not None
    assert actual["match"] is True
    assert actual["dx_px"] == 4.0


def test_run_case_returns_none_when_a_pointer_input_is_absent(tmp_path: Path) -> None:
    payload = json.loads(json.dumps(_VALID))
    payload["cases"][0]["inputs"] = {
        "before": "local_test_data/missing/frames/before.fits",
        "after": "local_test_data/missing/frames/after.fits",
    }
    manifest = load_manifest(_write_manifest(tmp_path, payload))

    assert run_case(manifest, manifest.cases[0]) is None


# --- assert_case_expectation ---------------------------------------------------


def _case(expect: dict[str, object], tolerances: dict[str, float]) -> RegressionCase:
    return RegressionCase(
        name="c", inputs={"before": "x"}, expect=expect, tolerances=tolerances, rationale="r"
    )


def test_assert_case_expectation_passes_within_tolerance() -> None:
    assert_case_expectation(
        {"match": True, "dx_px": 30.4}, _case({"match": True, "dx_px": 30.0}, {"dx_px": 1.0})
    )


def test_assert_case_expectation_fails_outside_tolerance() -> None:
    with pytest.raises(AssertionError, match="dx_px"):
        assert_case_expectation({"dx_px": 33.0}, _case({"dx_px": 30.0}, {"dx_px": 1.0}))


def test_assert_case_expectation_compares_untolerated_keys_exactly() -> None:
    with pytest.raises(AssertionError, match="match"):
        assert_case_expectation({"match": True}, _case({"match": False}, {}))
