"""scripts/prove.py (issue #54): proof-manifest schema validation, node-id checks and
manifest selection -- the pure parts, no git or pytest subprocesses."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "prove.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prove", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


prove = _load_script()

_FILES = {
    "apps/collimation_tool/ui/panel.py",
    "tests/collimation/ui/test_panel.py",
    "tests/core/x/test_y.py",
}


def _exists(path: str) -> bool:
    return path in _FILES


def _manifest(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401 - TOML values
    data: dict[str, Any] = {
        "issues": [49],
        "summary": "the UI froze",
        "modules": ["apps/collimation_tool/ui/panel.py"],
        "regression_tests": [
            {
                "node": "tests/collimation/ui/test_panel.py::TestA::test_b",
                "failed_before_fix": True,
                "evidence": "tests/collimation/ui/test_panel.py:12: assert False",
            }
        ],
        "dataset_uuids": ["6d33f37c"],
        "focused_command": "pytest -q tests/collimation/ui/test_panel.py::TestA",
        "broader_tier": "component",
        "field_only_assumptions": [],
        "fix_commits": ["fed00fa"],
    }
    data.update(overrides)
    return data


class TestValidateManifest:
    def test_a_complete_manifest_is_valid(self) -> None:
        assert prove.validate_manifest(_manifest(), _exists) == []

    def test_optional_gaps_are_accepted(self) -> None:
        assert prove.validate_manifest(_manifest(gaps=["no focuser test"]), _exists) == []

    @pytest.mark.parametrize("key", list(prove.REQUIRED_KEYS))
    def test_every_required_key_is_enforced(self, key: str) -> None:
        data = _manifest()
        del data[key]
        assert any(f"missing key {key!r}" in e for e in prove.validate_manifest(data, _exists))

    def test_an_unknown_key_is_rejected(self) -> None:
        errors = prove.validate_manifest(_manifest(owner="me"), _exists)
        assert "unknown key 'owner'" in errors

    def test_a_missing_module_or_a_non_production_path_is_rejected(self) -> None:
        errors = prove.validate_manifest(
            _manifest(modules=["apps/gone.py", "tests/core/x/test_y.py"]), _exists
        )
        assert "modules: 'apps/gone.py' does not exist" in errors
        assert "modules: 'tests/core/x/test_y.py' is not under packages/ or apps/" in errors

    def test_a_missing_test_file_is_rejected(self) -> None:
        test = {"node": "tests/gone.py::test_x", "failed_before_fix": True, "evidence": "e"}
        errors = prove.validate_manifest(
            _manifest(regression_tests=[test], focused_command="pytest tests/gone.py"), _exists
        )
        assert "regression_tests[0]: test file 'tests/gone.py' does not exist" in errors
        assert "focused_command: path 'tests/gone.py' does not exist" in errors

    def test_failed_before_fix_needs_evidence_and_not_failed_needs_a_reason(self) -> None:
        node = "tests/collimation/ui/test_panel.py::TestA::test_b"
        failed = {"node": node, "failed_before_fix": True}
        not_failed = {"node": node, "failed_before_fix": False}
        errors = prove.validate_manifest(_manifest(regression_tests=[failed]), _exists)
        assert any("failed_before_fix = true needs 'evidence'" in e for e in errors)
        assert (
            "regression_tests[0]: failed_before_fix = false needs 'reason_no_prefix_repro'"
            in prove.validate_manifest(_manifest(regression_tests=[not_failed]), _exists)
        )

    def test_failed_before_fix_must_be_a_boolean(self) -> None:
        test = {"node": "tests/core/x/test_y.py::test_z", "failed_before_fix": "yes"}
        errors = prove.validate_manifest(
            _manifest(regression_tests=[test], focused_command="pytest tests/core/x"), _exists
        )
        assert "regression_tests[0]: failed_before_fix must be true or false" in errors

    def test_the_focused_command_must_run_every_listed_regression_test(self) -> None:
        errors = prove.validate_manifest(
            _manifest(focused_command="pytest -q tests/core/x/test_y.py"), _exists
        )
        assert (
            "focused_command does not run regression test "
            "'tests/collimation/ui/test_panel.py::TestA::test_b'" in errors
        )

    def test_the_focused_command_must_be_pytest(self) -> None:
        errors = prove.validate_manifest(_manifest(focused_command="make test"), _exists)
        assert any("must start with 'pytest'" in e for e in errors)

    def test_issues_may_be_empty_only_with_a_field_report_uuid(self) -> None:
        assert prove.validate_manifest(_manifest(issues=[]), _exists) == []
        errors = prove.validate_manifest(_manifest(issues=[], dataset_uuids=[]), _exists)
        assert "issues: may be empty only when dataset_uuids names the field report" in errors

    @pytest.mark.parametrize("issues", [[0], ["49"], [True], 49])
    def test_issues_must_be_positive_numbers(self, issues: object) -> None:
        errors = prove.validate_manifest(_manifest(issues=issues), _exists)
        assert "issues: must be a list of positive issue numbers" in errors

    @pytest.mark.parametrize(
        "uuid", ["6d33f37c", "fe91004f-1e52-4d93-a69d-6fb8cc0d5e7d", "fe91004f-1e52"]
    )
    def test_a_uuid_or_its_eight_hex_prefix_is_accepted(self, uuid: str) -> None:
        assert prove.validate_manifest(_manifest(dataset_uuids=[uuid]), _exists) == []

    @pytest.mark.parametrize("uuid", ["6d33f3", "bundle-6d33f37c", "XYZ12345"])
    def test_a_malformed_uuid_is_rejected(self, uuid: str) -> None:
        errors = prove.validate_manifest(_manifest(dataset_uuids=[uuid]), _exists)
        assert any(e.startswith("dataset_uuids:") for e in errors)

    @pytest.mark.parametrize("commits", [[], ["abc"], ["not-a-sha!"], "fed00fa"])
    def test_fix_commits_must_be_commit_ids(self, commits: object) -> None:
        errors = prove.validate_manifest(_manifest(fix_commits=commits), _exists)
        assert "fix_commits: must be a non-empty list of 7-40 hex commit ids" in errors

    def test_an_unknown_broader_tier_is_rejected(self) -> None:
        errors = prove.validate_manifest(_manifest(broader_tier="everything"), _exists)
        assert any(e.startswith("broader_tier:") for e in errors)

    @pytest.mark.parametrize(
        "evidence",
        ["it failed", "revert-proof: the event loop blocked", "see line 481"],
    )
    def test_evidence_must_name_the_failing_line(self, evidence: str) -> None:
        test = {
            "node": "tests/collimation/ui/test_panel.py::TestA::test_b",
            "failed_before_fix": True,
            "evidence": evidence,
        }
        errors = prove.validate_manifest(_manifest(regression_tests=[test]), _exists)
        assert any("path.py:NNN" in e for e in errors)

    @pytest.mark.parametrize(
        "evidence",
        [
            "tests/collimation/ui/test_panel.py:12: assert False",
            r"C:\wt\tests\core\x\test_y.py:608: assert not True",
            "packages/astrotool_core/filter_wheel/adapter.py:97: ConnectionError",
        ],
    )
    def test_evidence_with_a_file_and_line_is_accepted(self, evidence: str) -> None:
        test = {
            "node": "tests/collimation/ui/test_panel.py::TestA::test_b",
            "failed_before_fix": True,
            "evidence": evidence,
        }
        assert prove.validate_manifest(_manifest(regression_tests=[test]), _exists) == []


_NODE = "tests/collimation/ui/test_panel.py::TestA::test_b"


def _command_errors(command: str) -> list[str]:
    return [
        e
        for e in prove.validate_manifest(_manifest(focused_command=command), _exists)
        if e.startswith("focused_command")
    ]


class TestFocusedCommand:
    def test_args_drop_the_pytest_word_and_keep_quoting(self) -> None:
        assert prove.focused_args("pytest -q 'tests/a b.py'") == ["-q", "tests/a b.py"]

    @pytest.mark.parametrize(
        "command",
        [
            f"pytest {_NODE}",
            f"pytest -q -x -v -qq -s -l --quiet --exitfirst {_NODE}",
            f"pytest -ra -rfE --tb=short --tb line --durations=5 --durations 3 {_NODE}",
            f"pytest --maxfail=1 --maxfail 2 -p no:randomly -pno:cacheprovider -r a {_NODE}",
        ],
    )
    def test_whitelisted_flags_and_their_values_are_accepted(self, command: str) -> None:
        assert _command_errors(command) == []

    @pytest.mark.parametrize(
        ("command", "bad"),
        [
            (f"pytest -k nothing {_NODE}", "-k"),
            (f"pytest -knothing {_NODE}", "-knothing"),
            (f"pytest -m hardware {_NODE}", "-m"),
            (f"pytest --deselect={_NODE} {_NODE}", f"--deselect={_NODE}"),
            (f"pytest --deselect {_NODE} {_NODE}", "--deselect"),
            (f"pytest --co {_NODE}", "--co"),
            (f"pytest --collect-only {_NODE}", "--collect-only"),
            (f"pytest --lf {_NODE}", "--lf"),
            (f"pytest --ff {_NODE}", "--ff"),
            (f"pytest --sw {_NODE}", "--sw"),
            (f"pytest --runxfail {_NODE}", "--runxfail"),
            (f"pytest -c other.ini {_NODE}", "-c"),
            (f"pytest --ignore=tests {_NODE}", "--ignore=tests"),
        ],
    )
    def test_every_other_option_is_rejected(self, command: str, bad: str) -> None:
        assert f"focused_command: option {bad!r} is not allowed (see proofs/README.md)" in (
            _command_errors(command)
        )

    @pytest.mark.parametrize(
        ("command", "message"),
        [
            (f"pytest -p myplugin {_NODE}", "focused_command: -p 'myplugin' is not allowed"),
            (f"pytest --tb=fancy {_NODE}", "focused_command: --tb 'fancy' is not allowed"),
            (f"pytest --maxfail=x {_NODE}", "focused_command: --maxfail 'x' is not allowed"),
            (f"pytest -rZ {_NODE}", "focused_command: -r 'Z' is not allowed"),
            (f"pytest {_NODE} --tb", "focused_command: option '--tb' needs a value"),
        ],
    )
    def test_a_whitelisted_option_with_a_bad_value_is_rejected(
        self, command: str, message: str
    ) -> None:
        assert message in _command_errors(command)

    def test_an_option_value_is_never_taken_for_a_path(self) -> None:
        paths, errors = prove.parse_focused_args(["--tb", "short", "-p", "no:x", "tests/a.py"])
        assert (paths, errors) == (["tests/a.py"], [])

    def test_a_command_with_no_test_path_is_rejected(self) -> None:
        assert "focused_command: names no test paths" in _command_errors("pytest -q")

    @pytest.mark.parametrize(
        ("node", "paths", "expected"),
        [
            ("tests/a.py::C::t", ["tests/a.py"], True),
            ("tests/a.py::C::t", ["tests/a.py::C"], True),
            ("tests/a.py::C::t", ["tests/a.py::C::t"], True),
            ("tests/a.py::C::t", ["tests/a.py::Cx"], False),
            ("tests/a.py::C::t", ["tests/b.py"], False),
        ],
    )
    def test_selected_by(self, node: str, paths: list[str], expected: bool) -> None:
        assert prove.selected_by(node, paths) is expected

    def test_node_file(self) -> None:
        assert prove.node_file("tests/a.py::C::t[p-1]") == "tests/a.py"
        assert prove.node_file("tests/a.py") == "tests/a.py"


class TestStaticNodeProblems:
    _SOURCE = (
        "import pytest\n\n"
        "class TestA:\n"
        "    def test_b(self) -> None:\n        pass\n\n"
        "    async def test_async(self) -> None:\n        pass\n\n"
        "    class TestInner:\n        def test_deep(self) -> None:\n            pass\n\n"
        "def test_c():\n    pass\n\n"
        "def test_only_top():\n    pass\n\n"
        "@pytest.mark.skip(reason='x')\ndef test_skipped():\n    pass\n\n"
        "@pytest.mark.skipif(True, reason='x')\ndef test_skipif():\n    pass\n\n"
        "@pytest.mark.xfail\ndef test_xfail():\n    pass\n\n"
        "@pytest.mark.hardware\ndef test_hw():\n    pass\n\n"
        "@pytest.mark.parametrize('a', [1])\ndef test_param(a):\n    pass\n\n"
        "@pytest.mark.xfail(strict=True)\nclass TestXfailed:\n"
        "    def test_x(self) -> None:\n        pass\n"
    )

    @pytest.mark.parametrize(
        "node",
        [
            "tests/t.py::TestA::test_b",
            "tests/t.py::TestA::test_async",
            "tests/t.py::TestA::TestInner::test_deep",
            "tests/t.py::TestA",
            "tests/t.py::test_c",
            "tests/t.py::test_c[x-1]",
            "tests/t.py::test_param[1]",
        ],
    )
    def test_existing_names_pass(self, node: str) -> None:
        assert prove.static_node_problems(node, self._SOURCE) == []

    def test_a_missing_function_or_class_is_reported(self) -> None:
        assert prove.static_node_problems("tests/t.py::TestA::test_gone", self._SOURCE) == [
            "tests/t.py::TestA::test_gone: no 'def test_gone' in tests/t.py::TestA"
        ]
        assert prove.static_node_problems("tests/t.py::TestGone::test_b", self._SOURCE) == [
            "tests/t.py::TestGone::test_b: no 'class TestGone' in tests/t.py"
        ]

    def test_a_function_must_be_defined_in_the_named_class(self) -> None:
        # test_only_top exists, but at module level -- not inside TestA
        assert prove.static_node_problems("tests/t.py::TestA::test_only_top", self._SOURCE) != []
        # test_deep exists, but only inside TestA::TestInner
        assert prove.static_node_problems("tests/t.py::TestA::test_deep", self._SOURCE) != []

    def test_a_prefix_of_a_longer_name_does_not_count(self) -> None:
        assert prove.static_node_problems("tests/t.py::test_", self._SOURCE) != []

    @pytest.mark.parametrize(
        ("node", "mark"),
        [
            ("tests/t.py::test_skipped", "pytest.mark.skip"),
            ("tests/t.py::test_skipif", "pytest.mark.skipif"),
            ("tests/t.py::test_xfail", "pytest.mark.xfail"),
            ("tests/t.py::test_hw", "pytest.mark.hardware"),
            ("tests/t.py::TestXfailed::test_x", "pytest.mark.xfail"),
        ],
    )
    def test_a_static_skip_xfail_or_hardware_marker_is_rejected(self, node: str, mark: str) -> None:
        assert prove.static_node_problems(node, self._SOURCE) == [
            f"{node}: carries static marker {mark!r}; it would not run in CI"
        ]

    def test_a_module_level_pytestmark_skip_is_rejected(self) -> None:
        source = "import pytest\npytestmark = pytest.mark.skip\n\ndef test_a():\n    pass\n"
        assert prove.static_node_problems("tests/t.py::test_a", source) == [
            "tests/t.py::test_a: carries static marker 'pytest.mark.skip'; it would not run in CI"
        ]


def _tier_for(relpath: str, name: str) -> str | None:
    if relpath.startswith("tests/nowhere/"):
        return None
    if name.startswith("test_real_"):
        return "hardware"
    return "integration" if relpath.startswith("tests/local_data/") else "unit"


class TestNodeTier:
    def test_a_ci_tier_passes(self) -> None:
        assert prove.node_tier_problems("tests/core/x/test_y.py::TestA::test_b", _tier_for) == []

    def test_a_hardware_test_is_rejected(self) -> None:
        node = "tests/contracts/test_c.py::test_real_device[a]"
        assert prove.node_tier_problems(node, _tier_for) == [
            f"{node}: tier 'hardware' does not run in CI"
        ]

    def test_local_only_data_is_rejected(self) -> None:
        assert prove.node_tier_problems("tests/local_data/test_x.py::test_a", _tier_for) != []

    def test_an_untiered_path_is_rejected(self) -> None:
        assert prove.node_tier_problems("tests/nowhere/test_x.py::test_a", _tier_for) != []

    def test_the_real_rule_comes_from_tests_conftest(self) -> None:
        tier_for = prove._load_tier_for()
        assert tier_for("tests/contracts/test_x.py", "test_real_a") == "hardware"
        assert tier_for("tests/collimation/ui/test_x.py", "test_a") == "component"


class TestSelection:
    _MANIFESTS = [
        prove.Manifest("47", {"issues": [47], "dataset_uuids": []}),
        prove.Manifest("47-foo", {"issues": [47], "dataset_uuids": []}),
        prove.Manifest("49", {"issues": [49], "dataset_uuids": ["6d33f37c"]}),
        prove.Manifest("74-other", {"issues": [12], "dataset_uuids": []}),
        prove.Manifest("diag-73c7d59d-stuck", {"issues": [47], "dataset_uuids": ["73c7d59d"]}),
        prove.Manifest(
            "diag-7b21bdf1-lock",
            {"issues": [], "dataset_uuids": ["7b21bdf1-0000-4000-8000-000000000000"]},
        ),
    ]

    def _ids(self, proof_id: str) -> list[str]:
        return [m.proof_id for m in prove.select_manifests(proof_id, self._MANIFESTS)]

    def test_a_bare_integer_always_means_every_manifest_listing_that_issue(self) -> None:
        # 47.toml AND 47-foo.toml AND the diag manifest listing 47 -- never just the stem
        assert self._ids("47") == ["47", "47-foo", "diag-73c7d59d-stuck"]
        assert self._ids("#47") == ["47", "47-foo", "diag-73c7d59d-stuck"]
        # a stem that happens to start with the number is NOT selected by it
        assert self._ids("74") == []

    def test_anything_else_is_an_exact_stem_first(self) -> None:
        assert self._ids("47-foo") == ["47-foo"]

    def test_a_unique_stem_prefix_selects_that_manifest(self) -> None:
        assert self._ids("diag-7b21bdf1") == ["diag-7b21bdf1-lock"]
        assert self._ids("74-o") == ["74-other"]

    def test_an_ambiguous_stem_prefix_lists_the_candidates(self) -> None:
        with pytest.raises(LookupError, match="diag-73c7d59d-stuck, diag-7b21bdf1-lock"):
            prove.select_manifests("diag-", self._MANIFESTS)

    def test_a_uuid_prefix_selects_a_manifest_without_issues(self) -> None:
        assert self._ids("7b21bdf1") == ["diag-7b21bdf1-lock"]
        assert self._ids("6d33f3") == ["49"]

    def test_an_unknown_id_selects_nothing(self) -> None:
        assert self._ids("99") == []
        assert self._ids("diag-gone") == []


class TestSelectionOfTheRealManifests:
    _REAL = prove.load_manifests()

    def _ids(self, proof_id: str) -> list[str]:
        return [m.proof_id for m in prove.select_manifests(proof_id, self._REAL)]

    def test_the_documented_examples_work(self) -> None:
        # A bare issue number selects every manifest whose `issues` lists it (README rule) --
        # pinned as the rule, not a frozen list, so a new manifest for #49 can't break it.
        listing_49 = {m.proof_id for m in self._REAL if 49 in m.issues}
        assert "49" in listing_49
        assert set(self._ids("49")) == listing_49
        assert self._ids("diag-7b21bdf1") == ["diag-7b21bdf1-onstep-serialization"]
        assert self._ids("7b21bdf1") == ["diag-7b21bdf1-onstep-serialization"]
        listing_47 = {m.proof_id for m in self._REAL if 47 in m.issues}
        assert {"47-efw-device-name", "diag-73c7d59d-filter-stuck-busy"} <= listing_47
        assert set(self._ids("47")) == listing_47

    def test_the_bare_diag_prefix_is_ambiguous(self) -> None:
        with pytest.raises(LookupError, match="ambiguous"):
            prove.select_manifests("diag-", self._REAL)


def test_summary_has_one_pass_or_fail_line_per_manifest() -> None:
    assert prove.summary_lines([("47-efw", 0), ("diag-x", 1), ("49", 5)]) == [
        "PASS  47-efw",
        "FAIL  diag-x",
        "FAIL  49",
    ]


def test_missing_commits_lists_only_the_unknown_ones() -> None:
    known = {"fed00fa"}
    assert prove.missing_commits(["fed00fa", "deadbee"], known.__contains__) == ["deadbee"]


def test_proof_env_puts_this_checkouts_code_first() -> None:
    root = Path("/repo")
    env = prove.proof_env({"PYTHONPATH": "/other", "X": "1"}, root)
    parts = env["PYTHONPATH"].split(prove.os.pathsep)
    assert parts == [str(root / "packages"), str(root / "apps"), "/other"]
    assert env["X"] == "1"
    assert prove.proof_env({}, root)["PYTHONPATH"].split(prove.os.pathsep) == [
        str(root / "packages"),
        str(root / "apps"),
    ]
