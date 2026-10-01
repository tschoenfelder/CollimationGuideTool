"""Run or validate an issue's proof manifest (issue #54).

A proof manifest (``proofs/<id>.toml``, schema in ``proofs/README.md``) names
the exact regression tests that would fail if a fixed defect returned, plus the
one focused command that runs them.

Usage::

    python scripts/prove.py 49              # every manifest whose `issues` lists #49
    python scripts/prove.py diag-7b21bdf1   # one manifest by file stem or unique stem prefix
    python scripts/prove.py 7b21bdf1        # ... or by a diagnostic UUID prefix
    python scripts/prove.py 49 --dry-run    # print the command(s), run nothing
    python scripts/prove.py --validate-all  # validate every manifest (CI fast job)
    python scripts/prove.py --list

Selection rule: a bare integer (optionally ``#49``) ALWAYS means every manifest
whose ``issues`` lists it. Anything else is an exact file stem, else a unique
stem prefix, else a unique ``dataset_uuids`` prefix; an ambiguous prefix is an
error listing the candidates.

Every selected manifest's focused command runs (one failure does not stop the
rest), a PASS/FAIL line per manifest is printed, and the exit code is non-zero
if any failed. The focused command runs with ``packages/`` and ``apps/`` of THIS
checkout first on ``PYTHONPATH``, so a proof run inside a separate git worktree
(a revert experiment) exercises that worktree's code, not the editable install.

The checking logic lives in pure functions (unit-tested in
``tests/core/testing/test_prove.py``); only ``main`` and the ``_git_*`` /
``_collect_*`` / ``_run_*`` / ``_load_*`` helpers touch git, pytest or files.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
PROOFS_DIR = REPO_ROOT / "proofs"

BROADER_TIERS = ("unit", "component", "contract", "integration", "acceptance", "release")
REQUIRED_KEYS = (
    "issues",
    "summary",
    "modules",
    "regression_tests",
    "dataset_uuids",
    "focused_command",
    "broader_tier",
    "field_only_assumptions",
    "fix_commits",
)
OPTIONAL_KEYS = ("gaps",)
TEST_KEYS_REQUIRED = ("node", "failed_before_fix")
TEST_KEYS_OPTIONAL = ("evidence", "reason_no_prefix_repro")

_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
# A full diagnostic UUID or its first >= 8 hex digits (bundles are referred to that way).
_UUID = re.compile(r"^[0-9a-f]{8}(-[0-9a-f]{4}){0,3}(-[0-9a-f]{12})?$")
_PRODUCTION_PREFIXES = ("packages/", "apps/")
# Evidence of a pre-fix failure must point at the failing line: `some/file.py:123`.
_EVIDENCE_LINE = re.compile(r"[\w./\\-]+\.py:\d+")

# focused_command flags: a whitelist. Anything that could deselect, filter or skip running
# the listed tests (-k, -m, --deselect, --co, --lf, ...) is rejected.
_BARE_SHORT = re.compile(r"^-[qvxsl]+$")  # -q, -qq, -v, -x, -s, -l and combinations
_BARE_LONG = frozenset({"--quiet", "--verbose", "--exitfirst", "--showlocals"})
_VALUE_OPTIONS: dict[str, Callable[[str], bool]] = {
    "--tb": lambda v: v in {"auto", "long", "short", "line", "native", "no"},
    "--durations": str.isdigit,
    "--maxfail": str.isdigit,
    "-r": lambda v: re.fullmatch(r"[fEsxXpPaAwN]+", v) is not None,
    "-p": lambda v: v.startswith("no:") and len(v) > 3,  # only DISABLING a plugin
}
# Tiers that do not run in CI's fast/slow jobs (hardware is skipped without opt-in).
_NOT_RUN_IN_CI_TIERS = frozenset({"hardware"})
# Local-only real-data tests: they skip when the dataset is absent, as on CI.
_LOCAL_ONLY_PREFIX = "tests/local_data/"
_STATIC_SKIP_MARK = re.compile(r"\bpytest\.mark\.(skip|skipif|xfail|hardware)\b")

TierFor = Callable[[str, str], "str | None"]


@dataclass(frozen=True)
class Manifest:
    proof_id: str
    data: Mapping[str, Any]

    @property
    def issues(self) -> list[int]:
        return list(self.data.get("issues", []))

    @property
    def nodes(self) -> list[str]:
        return [str(t["node"]) for t in self.data.get("regression_tests", [])]


# --------------------------------------------------------------------- pure logic


def _is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def node_file(node: str) -> str:
    """The test file of a pytest node id (``a.py::C::t[p]`` -> ``a.py``)."""
    return node.split("::", 1)[0]


def focused_args(command: str) -> list[str]:
    """The pytest arguments of a focused command (``pytest -q a b`` -> ``[-q, a, b]``).

    Raises ValueError unless the command is a plain ``pytest ...`` invocation."""
    parts = shlex.split(command)
    if not parts or parts[0] != "pytest":
        raise ValueError(f"focused_command must start with 'pytest': {command!r}")
    return parts[1:]


def _value_option(arg: str) -> tuple[str, str | None] | None:
    """(option, inline value or None) for a whitelisted value-taking option, else None."""
    for option in _VALUE_OPTIONS:
        if arg == option:
            return option, None
        if option.startswith("--") and arg.startswith(option + "="):
            return option, arg[len(option) + 1 :]
        if not option.startswith("--") and arg.startswith(option) and len(arg) > len(option):
            return option, arg[len(option) :]  # -ra, -pno:randomly
    return None


def parse_focused_args(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """(test path arguments, errors) of a focused command's pytest arguments.

    Only whitelisted flags are allowed; a value-taking option consumes its value (inline
    or the next argument), so the value is never mistaken for a test path."""
    paths: list[str] = []
    errors: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1
        if not arg.startswith("-"):
            paths.append(arg)
            continue
        if _BARE_SHORT.match(arg) or arg in _BARE_LONG:
            continue
        parsed = _value_option(arg)
        if parsed is None:
            errors.append(f"focused_command: option {arg!r} is not allowed (see proofs/README.md)")
            continue
        option, value = parsed
        if value is None:
            if index >= len(args):
                errors.append(f"focused_command: option {option!r} needs a value")
                continue
            value = args[index]
            index += 1
        if not _VALUE_OPTIONS[option](value):
            errors.append(f"focused_command: {option} {value!r} is not allowed")
    return paths, errors


def selected_by(node: str, paths: Iterable[str]) -> bool:
    """True when the pytest path arguments `paths` select `node` (the node itself, its file,
    or one of its parent node ids, e.g. its class)."""
    return any(node == p or node.startswith(p + "::") for p in paths)


def _validate_test(index: int, test: object, path_exists: Callable[[str], bool]) -> list[str]:
    where = f"regression_tests[{index}]"
    if not isinstance(test, dict):
        return [f"{where}: must be a table"]
    errors = [f"{where}: missing key {k!r}" for k in TEST_KEYS_REQUIRED if k not in test]
    unknown = set(test) - set(TEST_KEYS_REQUIRED) - set(TEST_KEYS_OPTIONAL)
    errors += [f"{where}: unknown key {k!r}" for k in sorted(unknown)]
    node = test.get("node")
    if not isinstance(node, str) or not node.startswith("tests/"):
        errors.append(f"{where}: node must be a pytest node id under tests/")
    elif not node_file(node).endswith(".py") or not path_exists(node_file(node)):
        errors.append(f"{where}: test file {node_file(node)!r} does not exist")
    failed = test.get("failed_before_fix")
    if not isinstance(failed, bool):
        errors.append(f"{where}: failed_before_fix must be true or false")
    elif failed and not _EVIDENCE_LINE.search(str(test.get("evidence", ""))):
        errors.append(
            f"{where}: failed_before_fix = true needs 'evidence' naming the failing "
            "line as path.py:NNN"
        )
    elif not failed and not str(test.get("reason_no_prefix_repro", "")).strip():
        errors.append(f"{where}: failed_before_fix = false needs 'reason_no_prefix_repro'")
    return errors


def _validate_lists(data: Mapping[str, Any], path_exists: Callable[[str], bool]) -> list[str]:
    errors: list[str] = []
    issues = data.get("issues")
    if not isinstance(issues, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in issues
    ):
        errors.append("issues: must be a list of positive issue numbers")
    uuids = data.get("dataset_uuids")
    if not isinstance(uuids, list) or not all(isinstance(u, str) and _UUID.match(u) for u in uuids):
        errors.append("dataset_uuids: must be a list of diagnostic UUIDs (or >= 8-hex prefixes)")
    elif isinstance(issues, list) and not issues and not uuids:
        errors.append("issues: may be empty only when dataset_uuids names the field report")
    modules = data.get("modules")
    if not _is_str_list(modules) or not modules:
        errors.append("modules: must be a non-empty list of production paths")
    else:
        for module in modules:
            if not module.startswith(_PRODUCTION_PREFIXES):
                errors.append(f"modules: {module!r} is not under packages/ or apps/")
            elif not path_exists(module):
                errors.append(f"modules: {module!r} does not exist")
    commits = data.get("fix_commits")
    if (
        not isinstance(commits, list)
        or not commits
        or not all(isinstance(c, str) and _COMMIT.match(c) for c in commits)
    ):
        errors.append("fix_commits: must be a non-empty list of 7-40 hex commit ids")
    for key in ("field_only_assumptions", "gaps"):
        if key in data and not (isinstance(data[key], list) and _is_str_list(data[key])):
            errors.append(f"{key}: must be a list of non-empty strings")
    return errors


def _validate_command(data: Mapping[str, Any], path_exists: Callable[[str], bool]) -> list[str]:
    command = data.get("focused_command")
    if not isinstance(command, str):
        return ["focused_command: must be a string"]
    try:
        paths, errors = parse_focused_args(focused_args(command))
    except ValueError as exc:
        return [str(exc)]
    if not paths:
        errors.append("focused_command: names no test paths")
    errors += [
        f"focused_command: path {node_file(p)!r} does not exist"
        for p in paths
        if not path_exists(node_file(p))
    ]
    tests = data.get("regression_tests")
    if isinstance(tests, list):
        for test in tests:
            node = test.get("node") if isinstance(test, dict) else None
            if isinstance(node, str) and not selected_by(node, paths):
                errors.append(f"focused_command does not run regression test {node!r}")
    return errors


def validate_manifest(data: Mapping[str, Any], path_exists: Callable[[str], bool]) -> list[str]:
    """Schema + file-existence problems of one manifest (empty list = valid).

    `path_exists` answers for a repo-relative posix path. Git commits, node ids and tiers
    are checked separately (`missing_commits`, `static_node_problems`, `node_tier_problems`)."""
    errors = [f"missing key {k!r}" for k in REQUIRED_KEYS if k not in data]
    unknown = set(data) - set(REQUIRED_KEYS) - set(OPTIONAL_KEYS)
    errors += [f"unknown key {k!r}" for k in sorted(unknown)]
    if not isinstance(data.get("summary"), str) or not data.get("summary", "").strip():
        errors.append("summary: must be a non-empty string")
    if data.get("broader_tier") not in BROADER_TIERS:
        errors.append(f"broader_tier: must be one of {', '.join(BROADER_TIERS)}")
    errors += _validate_lists(data, path_exists)
    tests = data.get("regression_tests")
    if not isinstance(tests, list) or not tests:
        errors.append("regression_tests: must be a non-empty list of tables")
    else:
        for index, test in enumerate(tests):
            errors += _validate_test(index, test, path_exists)
    errors += _validate_command(data, path_exists)
    return errors


_Definition = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _find(body: Iterable[ast.stmt], name: str, want_class: bool) -> _Definition | None:
    for statement in body:
        if want_class and isinstance(statement, ast.ClassDef) and statement.name == name:
            return statement
        if (
            not want_class
            and isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef)
            and statement.name == name
        ):
            return statement
    return None


def _skip_marks(decorators: Iterable[ast.expr]) -> list[str]:
    return [
        match.group(0)
        for decorator in decorators
        if (match := _STATIC_SKIP_MARK.search(ast.unparse(decorator)))
    ]


def _module_skip_marks(tree: ast.Module) -> list[str]:
    marks = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in statement.targets
        ):
            marks += _skip_marks([statement.value])
    return marks


def static_node_problems(node: str, source: str) -> list[str]:
    """Problems of a node id against its test file's `source`, without importing it:
    each ``::Class`` must be a class nested in the previous one (top-level first), the last
    part a ``def``/``async def`` in that scope, and no definition on the path (nor the
    module's ``pytestmark``) may carry a static skip/skipif/xfail/hardware marker -- such a
    test would not really run in CI. Parametrize ids (``[...]``) are ignored."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"{node}: {node_file(node)} does not parse: {exc}"]
    parts = [part.split("[", 1)[0] for part in node.split("::")[1:]]
    problems = []
    marks = _module_skip_marks(tree)
    body: Iterable[ast.stmt] = tree.body
    for depth, name in enumerate(parts):
        last = depth == len(parts) - 1
        # the last part is a test function, or (a class-level node id) a class
        found = _find(body, name, want_class=False) if last else None
        found = found or _find(body, name, want_class=True)
        if found is None:
            keyword = "def" if last else "class"
            scope = "::".join([node_file(node), *parts[:depth]])
            problems.append(f"{node}: no '{keyword} {name}' in {scope}")
            return problems
        marks += _skip_marks(found.decorator_list)
        body = found.body
    problems += [f"{node}: carries static marker {m!r}; it would not run in CI" for m in marks]
    return problems


def node_tier_problems(node: str, tier_for: TierFor) -> list[str]:
    """A listed regression test must run in a CI tier (CI runs the regression tests in their
    normal tiers, not every focused command): not `hardware`, not local-only data, and the
    path must have a tier at all. `tier_for` is tests/conftest.py's rule."""
    relpath = node_file(node)
    parts = node.split("::")[1:]
    name = parts[-1].split("[", 1)[0] if parts else ""
    tier = tier_for(relpath, name)
    if tier is None:
        return [f"{node}: no test tier for this path (tests/conftest.py tier_for)"]
    if tier in _NOT_RUN_IN_CI_TIERS:
        return [f"{node}: tier {tier!r} does not run in CI"]
    if relpath.startswith(_LOCAL_ONLY_PREFIX):
        return [f"{node}: {_LOCAL_ONLY_PREFIX} tests skip in CI (local data only)"]
    return []


def missing_commits(commits: Iterable[str], exists: Callable[[str], bool]) -> list[str]:
    return [c for c in commits if not exists(c)]


def select_manifests(proof_id: str, manifests: Sequence[Manifest]) -> list[Manifest]:
    """The manifests `proof_id` names. A bare integer (``49`` / ``#49``) means every manifest
    whose `issues` lists it. Anything else: the exact file stem, else a unique stem prefix,
    else a unique dataset-UUID prefix. Raises LookupError when a prefix is ambiguous."""
    bare = proof_id.removeprefix("#")
    if bare.isdigit():
        return [m for m in manifests if int(bare) in m.issues]
    exact = [m for m in manifests if m.proof_id == proof_id]
    if exact:
        return exact
    for candidates in (
        [m for m in manifests if m.proof_id.startswith(proof_id)],
        [
            m
            for m in manifests
            if any(str(u).startswith(proof_id) for u in m.data.get("dataset_uuids", []))
        ],
    ):
        if len(candidates) == 1:
            return candidates
        if len(candidates) > 1:
            names = ", ".join(m.proof_id for m in candidates)
            raise LookupError(f"{proof_id!r} is ambiguous: {names}")
    return []


def summary_lines(results: Sequence[tuple[str, int]]) -> list[str]:
    """One PASS/FAIL line per (manifest id, pytest exit code)."""
    return [f"{'PASS' if code == 0 else 'FAIL'}  {proof_id}" for proof_id, code in results]


def proof_env(environ: Mapping[str, str], repo_root: Path) -> dict[str, str]:
    """Environment for the focused run: this checkout's packages/apps first on PYTHONPATH."""
    env = dict(environ)
    ours = [str(repo_root / "packages"), str(repo_root / "apps")]
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([*ours, existing] if existing else ours)
    return env


# ------------------------------------------------------------------- I/O helpers


def load_manifests(proofs_dir: Path = PROOFS_DIR) -> list[Manifest]:
    return [
        Manifest(path.stem, tomllib.loads(path.read_text(encoding="utf-8")))
        for path in sorted(proofs_dir.glob("*.toml"))
    ]


def _load_tier_for(repo_root: Path = REPO_ROOT) -> TierFor:
    """tests/conftest.py's `tier_for` -- the one tier rule, imported rather than copied."""
    spec = importlib.util.spec_from_file_location(
        "_prove_tests_conftest", repo_root / "tests" / "conftest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tier_for: TierFor = module.tier_for
    return tier_for


def repo_path_exists(relpath: str, repo_root: Path = REPO_ROOT) -> bool:
    return (repo_root / relpath).is_file()


def _git_commit_exists(commit: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _git_is_shallow() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() == "true"


def static_problems(manifest: Manifest, repo_root: Path = REPO_ROOT) -> list[str]:
    """Schema, file, node-name, marker and tier problems of one manifest -- no subprocesses."""
    problems = validate_manifest(manifest.data, lambda p: repo_path_exists(p, repo_root))
    tier_for = _load_tier_for(repo_root)
    for node in manifest.nodes:
        path = repo_root / node_file(node)
        if path.is_file():
            problems += static_node_problems(node, path.read_text(encoding="utf-8"))
            problems += node_tier_problems(node, tier_for)
    return problems


def commit_problems(manifest: Manifest) -> tuple[list[str], list[str]]:
    """(errors, warnings): a missing fix commit is an error; only in a SHALLOW clone (where
    history is simply absent) is it a warning."""
    missing = missing_commits(manifest.data.get("fix_commits", []), _git_commit_exists)
    if not missing:
        return [], []
    if _git_is_shallow():
        return [], [
            f"SHALLOW CLONE -- fix commit {c} NOT VERIFIED (fetch full history to check)"
            for c in missing
        ]
    return [f"fix commit {c} not found in git" for c in missing], []


def _collect_problems(nodes: Sequence[str]) -> list[str]:
    """`pytest --collect-only` the listed node ids; any that fails to collect is a problem."""
    if not nodes:
        return []
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", *nodes],
        cwd=REPO_ROOT,
        env=proof_env(os.environ, REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return []
    tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-15:])
    return [f"pytest --collect-only failed for the listed regression tests:\n{tail}"]


def _run_focused(manifest: Manifest) -> int:
    args = focused_args(str(manifest.data["focused_command"]))
    command = [sys.executable, "-m", "pytest", *args]
    print(f"[prove {manifest.proof_id}] {' '.join(command)}", flush=True)
    return subprocess.run(
        command, cwd=REPO_ROOT, env=proof_env(os.environ, REPO_ROOT), check=False
    ).returncode


def _report(manifest: Manifest, errors: Sequence[str], warnings: Sequence[str]) -> None:
    for warning in warnings:
        print(f"proofs/{manifest.proof_id}.toml: WARNING: {warning}", file=sys.stderr)
    for error in errors:
        print(f"proofs/{manifest.proof_id}.toml: {error}", file=sys.stderr)


def validate_all(manifests: Sequence[Manifest], *, collect: bool = True) -> int:
    if not manifests:
        print("no proof manifests found in proofs/", file=sys.stderr)
        return 1
    failures = 0
    for manifest in manifests:
        errors, warnings = commit_problems(manifest)
        errors = static_problems(manifest) + errors
        _report(manifest, errors, warnings)
        failures += bool(errors)
    if collect:
        problems = _collect_problems(sorted({n for m in manifests for n in m.nodes}))
        for problem in problems:
            print(problem, file=sys.stderr)
        failures += len(problems)
    if failures:
        return 1
    print(f"{len(manifests)} proof manifests valid")
    return 0


def _prove(selected: Sequence[Manifest], *, dry_run: bool) -> int:
    results: list[tuple[str, int]] = []
    for manifest in selected:
        errors, warnings = commit_problems(manifest)
        errors = static_problems(manifest) + errors
        _report(manifest, errors, warnings)
        if errors:
            results.append((manifest.proof_id, 1))
        elif dry_run:
            args = focused_args(str(manifest.data["focused_command"]))
            print(f"[prove {manifest.proof_id}] pytest {shlex.join(args)}")
        else:
            results.append((manifest.proof_id, _run_focused(manifest)))
    if results:
        print("\nproof summary:")
        for line in summary_lines(results):
            print(f"  {line}")
    return 1 if any(code != 0 for _id, code in results) else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("proof_id", nargs="?", help="issue number, manifest stem/prefix or UUID")
    parser.add_argument("--validate-all", action="store_true", help="validate every manifest")
    parser.add_argument("--no-collect", action="store_true", help="skip pytest --collect-only")
    parser.add_argument("--dry-run", action="store_true", help="print the command only")
    parser.add_argument("--list", action="store_true", help="list manifests")
    options = parser.parse_args(argv)
    manifests = load_manifests()
    if options.list:
        for m in manifests:
            summary = " ".join(str(m.data.get("summary", "")).split())
            print(f"{m.proof_id:38} issues={m.issues} {summary[:70]}")
        return 0
    if options.validate_all:
        return validate_all(manifests, collect=not options.no_collect)
    if not options.proof_id:
        parser.error("give an issue number / manifest id, or --validate-all")
    try:
        selected = select_manifests(options.proof_id, manifests)
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 2
    if not selected:
        print(f"no proof manifest for {options.proof_id!r} in proofs/", file=sys.stderr)
        return 2
    return _prove(selected, dry_run=options.dry_run)


if __name__ == "__main__":
    sys.exit(main())
