"""scripts/changed_tests.py (issue #50): changed files -> the test paths that
prove them, or ALL_FAST when the mapping isn't known."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "changed_tests.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("changed_tests", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ct = _load_script()

# A miniature tests/ tree: the only test files the mapping may "see".
_TEST_FILES = {
    "tests/conftest.py",
    "tests/core/target/test_roi.py",
    "tests/core/target/test_roi_tracker.py",
    "tests/core/target/test_stacking.py",
    "tests/core/camera/test_touptek_adapter_identity.py",
    "tests/core/camera/test_touptek_adapter_no_hardware.py",
    "tests/core/camera/test_replay_camera.py",
    "tests/core/onstep/test_onstep_adapters.py",
    "tests/core/registration/test_terrestrial_registrar_real_data.py",
    "tests/core/testing/test_changed_tests.py",
    "tests/core/testing/test_prove.py",
    "tests/contracts/test_camera_contract.py",
    "tests/contracts/test_proof_manifests.py",
    "tests/collimation/test_onstep_boundary.py",
    "tests/collimation/domain/test_collimation_state.py",
    "tests/collimation/ui/conftest.py",
    "tests/collimation/ui/test_filter_wheel_panel.py",
    "tests/collimation/ui/test_live_view.py",
    "tests/guide/application/test_guide_controller.py",
    "tests/guide/domain/test_drift_estimator.py",
    "tests/integration/test_roi_tracker_replay.py",
    "tests/regressions/_loader.py",
    "tests/regressions/test_loader.py",
    "tests/acceptance/test_guiding_regression.py",
}


_PROOF_TESTS = ["tests/contracts/test_proof_manifests.py", "tests/core/testing/test_prove.py"]


def _map(*paths: str) -> list[str] | str:
    result: list[str] | str = ct.map_changed_paths(paths, _TEST_FILES)
    return result


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        # a module with its own test module(s) -> only those
        ("packages/astrotool_core/target/stacking.py", ["tests/core/target/test_stacking.py"]),
        (
            "packages/astrotool_core/target/roi.py",  # test_roi.py and test_roi_*.py
            ["tests/core/target/test_roi.py", "tests/core/target/test_roi_tracker.py"],
        ),
        (
            "apps/collimation_tool/domain/collimation_state.py",
            ["tests/collimation/domain/test_collimation_state.py"],
        ),
        (
            "apps/collimation_tool/ui/filter_wheel_panel.py",
            ["tests/collimation/ui/test_filter_wheel_panel.py"],
        ),
        (
            "apps/guide_tool/application/guide_controller.py",
            ["tests/guide/application/test_guide_controller.py"],
        ),
        # no own test module (or a package __init__) -> the layer/subsystem dir
        ("apps/collimation_tool/ui/flow_widget_helper.py", ["tests/collimation/ui"]),
        ("apps/guide_tool/domain/__init__.py", ["tests/guide/domain"]),
        ("packages/astrotool_core/target/detector.py", ["tests/core/target"]),
        # app entry point -> the whole app suite
        ("apps/collimation_tool/main.py", ["tests/collimation"]),
        # tests
        (
            "tests/guide/domain/test_drift_estimator.py",
            ["tests/guide/domain/test_drift_estimator.py"],
        ),
        ("tests/regressions/_loader.py", ["tests/regressions"]),
        ("tests/collimation/ui/conftest.py", ["tests/collimation/ui"]),
        ("scripts/changed_tests.py", ["tests/core/testing/test_changed_tests.py"]),
        # issue #54: a proof manifest, its README or its runner -> the proof tests
        ("proofs/49.toml", _PROOF_TESTS),
        ("proofs/README.md", _PROOF_TESTS),
        ("scripts/prove.py", _PROOF_TESTS),
    ],
)
def test_a_mappable_change_selects_only_its_own_tests(changed: str, expected: list[str]) -> None:
    assert _map(changed) == expected


@pytest.mark.parametrize(
    ("changed", "own_tests"),
    [
        (
            "packages/astrotool_core/camera/touptek_adapter.py",
            [
                "tests/core/camera/test_touptek_adapter_identity.py",
                "tests/core/camera/test_touptek_adapter_no_hardware.py",
            ],
        ),
        ("packages/astrotool_core/camera/port.py", ["tests/core/camera"]),
        ("packages/astrotool_core/camera/no_camera.py", ["tests/core/camera"]),
        (
            "packages/astrotool_core/camera/replay_camera.py",
            ["tests/core/camera/test_replay_camera.py"],
        ),
        ("packages/astrotool_core/onstep/settings.py", ["tests/core/onstep"]),
    ],
)
def test_a_changed_port_adapter_or_null_object_adds_the_contract_tests(
    changed: str, own_tests: list[str]
) -> None:
    assert _map(changed) == sorted([*own_tests, "tests/contracts"])


def test_a_non_boundary_core_module_does_not_run_the_contracts() -> None:
    assert _map("packages/astrotool_core/target/stacking.py") == [
        "tests/core/target/test_stacking.py"
    ]


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ("datasets/regressions/35/frame.png", ["tests/regressions"]),
        ("datasets/regressions/35/notes.txt", ["tests/regressions"]),
        ("datasets/regressions/35/case.json", ["tests/regressions"]),
        ("datasets/acceptance/case.json", ["tests/acceptance"]),
        ("datasets/guiding/lost_star/f.fits", ["tests/acceptance", "tests/integration"]),
        ("datasets/collimation/x/f.fits", ["tests/integration"]),
        (
            "datasets/fov_registration/a.fits",
            ["tests/core/registration/test_terrestrial_registrar_real_data.py"],
        ),
        (
            "datasets/brand_new_kind/a.json",
            ["tests/acceptance", "tests/integration", "tests/regressions"],
        ),
    ],
)
def test_a_dataset_change_selects_the_tests_reading_that_dataset(
    changed: str, expected: list[str]
) -> None:
    assert _map(changed) == expected


@pytest.mark.parametrize(
    "changed",
    [
        "tests/conftest.py",
        "tests/core/target/data.txt",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "packages/astrotool_core/testing/fake_mount.py",
        "packages/astrotool_core/camera/fake_camera.py",
        "packages/astrotool_core/filter_wheel/fake_filter_wheel.py",
        "packages/astrotool_core/__init__.py",
        "apps/collimation_tool/something/new.py",
        "scripts/quality_report.py",
        "datasets/README",
        # mapped, but the test directory doesn't exist -> mapping unknown
        "packages/astrotool_core/brand_new_subsystem/thing.py",
    ],
)
def test_shared_infra_or_an_unknown_path_means_run_all_fast(changed: str) -> None:
    assert _map(changed) == ct.ALL_FAST


def test_one_unknown_path_among_known_ones_still_means_run_all_fast() -> None:
    assert _map("packages/astrotool_core/target/roi.py", "pyproject.toml") == ct.ALL_FAST


@pytest.mark.parametrize(
    "changed", ["README.md", "docs/quality/test-tier-timings.md", ".github/workflows/x.yml"]
)
def test_docs_only_changes_select_no_tests(changed: str) -> None:
    assert _map(changed) == []


def test_several_changes_are_merged_sorted_and_deduplicated() -> None:
    assert _map(
        "packages/astrotool_core/target/stacking.py",
        "apps/collimation_tool/domain/collimation_state.py",
        "packages/astrotool_core/target/stacking.py",
        "",
    ) == [
        "tests/collimation/domain/test_collimation_state.py",
        "tests/core/target/test_stacking.py",
    ]


def test_a_selected_directory_absorbs_its_own_files() -> None:
    assert _map(
        "apps/collimation_tool/ui/filter_wheel_panel.py",
        "tests/collimation/ui/conftest.py",
    ) == ["tests/collimation/ui"]


def test_windows_separators_are_accepted() -> None:
    assert _map("packages\\astrotool_core\\target\\stacking.py") == [
        "tests/core/target/test_stacking.py"
    ]


def test_a_deleted_test_file_is_dropped_not_run() -> None:
    assert _map("tests/core/target/test_gone.py") == []


def test_output_is_lf_terminated_one_path_per_line() -> None:
    assert ct.format_output(["tests/a", "tests/b"]) == "tests/a\ntests/b\n"
    assert ct.format_output(ct.ALL_FAST) == "ALL_FAST\n"
    assert ct.format_output([]) == ""


def test_the_cli_writes_no_carriage_returns() -> None:
    """check.sh word-splits this output; under Git Bash on Windows a text-mode
    ``\\r\\n`` would leave ``\\r`` glued to every path."""
    out = subprocess.run(
        [sys.executable, str(_SCRIPT)], capture_output=True, check=True, cwd=_SCRIPT.parents[1]
    ).stdout
    assert b"\r" not in out
    assert out == b"" or out.endswith(b"\n")
