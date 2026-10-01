"""Select the tests that prove a change: map changed files to test paths.

Used by the default mode of ``scripts/check.ps1`` / ``scripts/check.sh``
(issue #50). Prints one test path per line (always ``\\n``-terminated, also
on Windows), the single token ``ALL_FAST`` when the change can't be mapped
safely (the caller then runs every fast tier:
``-m "unit or component or contract"``), or nothing when no changed file
needs a test run (docs only).

Changed files = everything differing from the merge-base with
``origin/main`` (committed, staged and unstaged) plus untracked files.

Mapping rules (see :func:`map_changed_paths`):

- a production module ``x.py`` -> its own test modules ``test_x.py`` /
  ``test_x_*.py`` in the matching test directory; only when none exist ->
  that whole directory:
  ``packages/astrotool_core/<sub>/`` -> ``tests/core/<sub>/``,
  ``apps/<app>/<layer>/`` -> ``tests/<app-tests>/<layer>/``;
- plus ``tests/contracts`` for a changed port/adapter/null object (or
  anything in ``onstep``/``indi``);
- an app's top-level module (``main.py``) -> the whole ``tests/<app-tests>``;
- a test file -> itself; a test helper (``tests/**/_x.py``) or a
  ``conftest.py`` below ``tests/<dir>/`` -> that directory;
- ``datasets/<kind>/**`` -> the tests that read that kind;
- this script -> its own unit tests;
- ``proofs/**`` or ``scripts/prove.py`` (issue #54) -> the proof-manifest
  contract test and prove.py's unit tests;
- docs / markdown / ``.github`` (outside ``tests/`` and ``datasets/``) -> no
  tests;
- anything else (``tests/conftest.py``, ``pyproject.toml``,
  ``requirements*.txt``, shared test doubles -- ``astrotool_core.testing`` or
  any ``fake_*.py`` --, unknown paths, mapped test dirs that don't exist)
  -> ``ALL_FAST``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Collection, Iterable
from pathlib import Path, PurePosixPath

ALL_FAST = "ALL_FAST"

REPO_ROOT = Path(__file__).resolve().parents[1]

_APP_TESTS = {"collimation_tool": "tests/collimation", "guide_tool": "tests/guide"}
_APP_LAYERS = {"application", "domain", "ui"}
# Every file in these core subpackages is a hardware boundary.
_BOUNDARY_SUBPACKAGES = {"onstep", "indi"}
# Elsewhere, only ports, adapters, null objects and the replay camera are.
_BOUNDARY_FILE = re.compile(r"(^|_)(port|adapter)\.py$|^no_|^replay_camera\.py$")
# datasets/<kind>/ -> the tests reading it; an unknown kind -> every consumer.
_DATASET_TESTS = {
    "regressions": ("tests/regressions",),
    "acceptance": ("tests/acceptance",),
    "guiding": ("tests/integration", "tests/acceptance"),
    "collimation": ("tests/integration",),
    "fov_registration": ("tests/core/registration/test_terrestrial_registrar_real_data.py",),
}
_ALL_DATASET_TESTS = ("tests/integration", "tests/regressions", "tests/acceptance")
_NO_TEST_SUFFIXES = {".md", ".txt", ".rst", ".png", ".jpg", ".svg"}
_NO_TEST_PREFIXES = ("docs/", ".github/", "wiki/")
_SELF = "scripts/changed_tests.py"
_SELF_TESTS = "tests/core/testing/test_changed_tests.py"
# Issue #54: proof manifests and their runner -> the tests that validate them.
_PROOF_TESTS = ("tests/contracts/test_proof_manifests.py", "tests/core/testing/test_prove.py")


class _RunAllFast(Exception):
    pass


def _module_tests(test_dir: str, module: str, test_files: Collection[str]) -> set[str]:
    """``test_<module>.py`` / ``test_<module>_*.py`` in ``test_dir``, else the dir."""
    stem = PurePosixPath(module).stem
    own = {
        f
        for f in test_files
        if PurePosixPath(f).parent.as_posix() == test_dir
        and (PurePosixPath(f).name == f"test_{stem}.py" or f"/test_{stem}_" in f)
    }
    if stem != "__init__" and own:
        return own
    return {test_dir}


def _map_one(path: PurePosixPath, test_files: Collection[str]) -> set[str]:
    parts = path.parts
    text = path.as_posix()
    if parts[0] == "datasets":
        if len(parts) < 3:
            raise _RunAllFast
        return set(_DATASET_TESTS.get(parts[1], _ALL_DATASET_TESTS))
    if path.name.startswith("requirements") or text == "pyproject.toml":
        raise _RunAllFast
    if parts[0] == "proofs" or text == "scripts/prove.py":
        return set(_PROOF_TESTS)
    if parts[0] != "tests" and (
        path.suffix in _NO_TEST_SUFFIXES or text.startswith(_NO_TEST_PREFIXES)
    ):
        return set()
    if text == _SELF:
        return {_SELF_TESTS}
    if parts[0] == "tests":
        if text == "tests/conftest.py" or path.suffix != ".py":
            raise _RunAllFast
        if path.name.startswith("test_"):
            return {text}
        return {path.parent.as_posix()}  # helper module or a nested conftest.py
    return _map_production(path, test_files)


def _map_production(path: PurePosixPath, test_files: Collection[str]) -> set[str]:
    parts = path.parts
    if parts[:2] == ("packages", "astrotool_core") and len(parts) >= 4:
        sub = parts[2]
        if sub == "testing" or path.name.startswith("fake_"):
            raise _RunAllFast  # shared test doubles: used far beyond their subpackage
        tests = _module_tests(f"tests/core/{sub}", path.name, test_files)
        if sub in _BOUNDARY_SUBPACKAGES or _BOUNDARY_FILE.search(path.name):
            tests.add("tests/contracts")
        return tests
    if parts[0] == "apps" and len(parts) >= 3 and parts[1] in _APP_TESTS:
        app_tests = _APP_TESTS[parts[1]]
        if len(parts) == 4 and parts[2] in _APP_LAYERS:
            return _module_tests(f"{app_tests}/{parts[2]}", path.name, test_files)
        if len(parts) > 4 and parts[2] in _APP_LAYERS:
            return {f"{app_tests}/{parts[2]}"}
        if len(parts) == 3:
            return {app_tests}
    raise _RunAllFast


def _present(target: str, test_files: Collection[str]) -> bool:
    return target in test_files or any(f.startswith(target + "/") for f in test_files)


def map_changed_paths(changed: Iterable[str], test_files: Collection[str]) -> list[str] | str:
    """Map changed repo-relative paths to the test paths that prove them.

    ``test_files`` is every ``tests/**/*.py`` file currently in the working
    tree (posix, repo-relative). Returns a sorted list of test paths, or
    :data:`ALL_FAST` when any path can't be mapped safely. A deleted test
    file is dropped; a mapped test path that doesn't exist means the mapping
    is unknown -> ALL_FAST.
    """
    selected: set[str] = set()
    try:
        for raw in changed:
            raw = raw.strip().replace("\\", "/")
            if not raw:
                continue
            path = PurePosixPath(raw)
            is_test_file = path.parts[0] == "tests" and path.name.startswith("test_")
            if is_test_file and raw not in test_files:
                continue  # deleted test: nothing left to run
            for target in _map_one(path, test_files):
                if not _present(target, test_files):
                    raise _RunAllFast
                selected.add(target)
    except _RunAllFast:
        return ALL_FAST
    # a directory already selected makes its own files redundant
    dirs = {s for s in selected if not s.endswith(".py")}
    return sorted(s for s in selected if not any(s.startswith(d + "/") for d in dirs))


def _git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def changed_files(base: str) -> list[str] | None:
    """Files changed vs the merge-base with ``base`` plus untracked files;
    None when git can't tell (no such ref, not a repo)."""
    try:
        merge_base = _git("merge-base", base, "HEAD")[0]
        return _git("diff", "--name-only", merge_base) + _git(
            "ls-files", "--others", "--exclude-standard"
        )
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return None


def format_output(result: list[str] | str) -> str:
    """What the CLI prints: ``\\n``-separated, never ``\\r\\n`` -- check.sh
    word-splits it, and a trailing ``\\r`` would become part of a path."""
    lines = [result] if isinstance(result, str) else result
    return "".join(f"{line}\n" for line in lines)


def main(argv: list[str]) -> int:
    base = argv[1] if len(argv) > 1 else "origin/main"
    changed = changed_files(base)
    if changed is None:
        result: list[str] | str = ALL_FAST
    else:
        test_files = {
            p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "tests").rglob("*.py")
        }
        result = map_changed_paths(changed, test_files)
    sys.stdout.buffer.write(format_output(result).encode("utf-8"))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
