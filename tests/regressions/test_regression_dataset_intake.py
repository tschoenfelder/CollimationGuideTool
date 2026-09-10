"""Unit tests for ``scripts/regression_dataset.py`` — the bundle → dataset
intake scaffolder (issue #6).

Runs the script's ``main()`` against a hand-built fake diagnostic bundle in
``tmp_path``, with ``REGRESSIONS_DIR`` and ``find_bundle`` monkeypatched so
nothing touches the real repo tree or the real diagnostics directory.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest
from _loader import load_manifest
from astropy.io import fits

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "regression_dataset.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("regression_dataset", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # so @dataclass in the module can resolve it
    spec.loader.exec_module(module)
    return module


rds = _load_script()

_FAKE_UUID = "ef49ecb1-a052-44ea-b45e-01145a9f0c33"


def _fake_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "frames").mkdir(parents=True)
    for index, name in enumerate(("axis1_before.fits", "axis1_after.fits")):
        fits.PrimaryHDU(data=np.full((4, 4), float(index), dtype=np.float32)).writeto(
            bundle / "frames" / name
        )
    (bundle / "incident.json").write_text(
        json.dumps(
            {
                "uuid": _FAKE_UUID,
                "git_commit": "185b94d472c6",
                "reason": "Failed for guide calibration but not for main",
                "context": {
                    "left": {"model": "ATR585M"},
                    "right": {"model": "GPCMOS02000KPA"},
                },
            }
        ),
        encoding="utf-8",
    )
    (bundle / "application.log").write_text("a log line\n", encoding="utf-8")
    return bundle


@pytest.fixture
def out_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bundle = _fake_bundle(tmp_path)
    regressions = tmp_path / "regressions"
    monkeypatch.setattr(rds, "REGRESSIONS_DIR", regressions)
    monkeypatch.setattr(rds, "find_bundle", lambda _uuid: bundle)
    return regressions


def test_scaffolds_a_full_dataset_directory(out_dir: Path) -> None:
    rc = rds.main(["--uuid", "ef49ecb1", "--issue", "34"])

    assert rc == 0
    dataset = out_dir / "34"
    assert (dataset / "frames" / "axis1_before.fits").is_file()
    assert (dataset / "frames" / "axis1_after.fits").is_file()
    assert (dataset / "provenance" / "incident.json").is_file()
    assert (dataset / "provenance" / "application.log").is_file()

    manifest = json.loads((dataset / "expected.json").read_text(encoding="utf-8"))
    assert manifest["issue"] == 34
    assert manifest["diagnostic_uuids"] == [_FAKE_UUID]
    assert manifest["git_commit_at_capture"] == "185b94d472c6"
    assert manifest["user_report"] == "Failed for guide calibration but not for main"
    assert manifest["optical_setup"] == "left: ATR585M; right: GPCMOS02000KPA"
    assert manifest["boundary"] == "TODO"
    case = manifest["cases"][0]
    # Ordering of the pre-filled inputs is arbitrary (the human repoints them
    # in step 1) — just confirm both real frames were wired in, not TODO stubs.
    assert set(case["inputs"]) == {"before", "after"}
    assert set(case["inputs"].values()) == {
        "frames/axis1_before.fits",
        "frames/axis1_after.fits",
    }
    assert "DO NOT copy" in case["rationale"]

    readme = (dataset / "README.md").read_text(encoding="utf-8")
    assert "185b94d472c6" in readme
    assert "## Root cause" in readme


def test_scaffold_output_is_loadable_by_the_manifest_parser(out_dir: Path) -> None:
    rds.main(["--uuid", "ef49ecb1", "--issue", "34"])

    # Parses cleanly (origin + non-empty boundary string + a case with a
    # rationale) — it just isn't *wired* yet (boundary == "TODO"), which is
    # exactly what the dead-data guard is meant to catch.
    manifest = load_manifest(out_dir / "34")
    assert manifest.issue == 34
    assert manifest.boundary == "TODO"


def test_diag_slug_fallback_when_no_issue_given(out_dir: Path) -> None:
    rc = rds.main(["--uuid", _FAKE_UUID])

    assert rc == 0
    assert (out_dir / "diag-ef49ecb1" / "expected.json").is_file()
    manifest = json.loads((out_dir / "diag-ef49ecb1" / "expected.json").read_text())
    assert manifest["issue"] is None
    assert manifest["diagnostic_uuids"] == [_FAKE_UUID]


def test_refuses_an_existing_directory_without_force(out_dir: Path) -> None:
    (out_dir / "34").mkdir(parents=True)
    (out_dir / "34" / "keep.txt").write_text("original", encoding="utf-8")

    rc = rds.main(["--uuid", "ef49ecb1", "--issue", "34"])

    assert rc == 2
    assert (out_dir / "34" / "keep.txt").read_text(encoding="utf-8") == "original"
    assert not (out_dir / "34" / "expected.json").exists()


def test_force_overwrites_an_existing_directory(out_dir: Path) -> None:
    (out_dir / "34").mkdir(parents=True)

    rc = rds.main(["--uuid", "ef49ecb1", "--issue", "34", "--force"])

    assert rc == 0
    assert (out_dir / "34" / "expected.json").is_file()


def test_missing_bundle_returns_exit_code_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rds, "REGRESSIONS_DIR", tmp_path / "regressions")
    monkeypatch.setattr(rds, "find_bundle", lambda _uuid: None)

    assert rds.main(["--uuid", "nope"]) == 2
