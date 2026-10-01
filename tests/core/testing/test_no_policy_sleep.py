"""Sleep guard (issue #53, CONTRIBUTING.md "Deterministic time").

Production code must not block on the wall clock directly: policy waits
(settle times, retries, confirmation timeouts, deadlines) go through an
injected `astrotool_core.timing.Clock`. This test AST-scans every production
module under `apps/` and `packages/astrotool_core/` for a direct call to the
standard library's sleep (`time.sleep`, `from time import sleep`, aliased
forms) and fails on any file not in `ALLOWLIST`.

Every allowlisted file states why. An entry that no longer sleeps fails too,
so the list only ever shrinks. To add an exception you need a real reason a
reviewer will accept (an external API that requires blocking), not "it was
simpler".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Scanned production trees (all of them; the policy layers named in
#: CONTRIBUTING.md and the adapters, whose exceptions are listed below).
SCANNED_ROOTS = ("apps", "packages/astrotool_core")

#: Test doubles and the clock itself are not production policy code.
EXCLUDED_PREFIXES = ("packages/astrotool_core/testing/",)

ALLOWLIST: dict[str, str] = {
    "packages/astrotool_core/timing/clock.py": (
        "SystemClock is the one sanctioned production sleep: everything else waits through it"
    ),
    "packages/astrotool_core/camera/touptek_adapter.py": (
        "hardware adapter: _prepare_capture_mode lets the SDK settle 0.2 s in video mode "
        "after StartPullModeWithCallback before switching to software trigger (without it: "
        "stale/black frames, smart_telescope M10 hardware history); vendor-API blocking"
    ),
    "apps/collimation_tool/ui/mount_test_move_runner.py": (
        "pending S3b (after S6.0): park/unpark polling, pulse-rejection retries and "
        "settle waits move to the injected clock once the Mount Align movement path is fixed"
    ),
    "apps/collimation_tool/application/recenter_policy.py": (
        "pending S3b (after S6.0): reacquisition settle wait moves to the injected clock"
    ),
}


def _production_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        for path in sorted((_REPO_ROOT / root).rglob("*.py")):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if "__pycache__" in rel or rel.startswith(EXCLUDED_PREFIXES):
                continue
            files.append(path)
    return files


def direct_sleep_calls(source: str) -> list[int]:
    """Line numbers of direct stdlib sleep calls in `source`: `time.sleep` calls
    (also via `import time as t`) and `sleep(...)` imported with
    `from time import sleep [as x]`. Passing a sleep function around as a
    value (e.g. a default argument) also counts: it is still a wall-clock wait."""
    tree = ast.parse(source)
    module_aliases: set[str] = set()
    function_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    module_aliases.add(alias.asname or "time")
        elif isinstance(node, ast.ImportFrom) and node.module == "time":
            for alias in node.names:
                if alias.name == "sleep":
                    function_aliases.add(alias.asname or "sleep")
    lines: list[int] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr == "sleep"
            and isinstance(node.value, ast.Name)
            and node.value.id in module_aliases
        ) or (
            isinstance(node, ast.Name)
            and node.id in function_aliases
            and isinstance(node.ctx, ast.Load)
        ):
            lines.append(node.lineno)
    return sorted(set(lines))


def _offenders() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in _production_files():
        lines = direct_sleep_calls(path.read_text(encoding="utf-8"))
        if lines:
            found[path.relative_to(_REPO_ROOT).as_posix()] = lines
    return found


def test_no_direct_sleep_outside_the_allowlist() -> None:
    unexpected = {path: lines for path, lines in _offenders().items() if path not in ALLOWLIST}
    assert not unexpected, (
        "Direct wall-clock sleep in production code (issue #53). Wait through an injected "
        "astrotool_core.timing.Clock instead, or add a justified ALLOWLIST entry: "
        f"{unexpected}"
    )


def test_every_allowlist_entry_still_sleeps() -> None:
    offenders = _offenders()
    stale = sorted(path for path in ALLOWLIST if path not in offenders)
    assert not stale, f"Remove these ALLOWLIST entries, they no longer sleep: {stale}"


def test_every_allowlist_entry_gives_a_reason() -> None:
    assert all(reason.strip() for reason in ALLOWLIST.values())


_SLEEP = "sleep"  # kept out of literal call syntax so the tier scanner ignores this file


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (f"import time\ntime.{_SLEEP}(1)\n", [2]),
        (f"import time as t\nt.{_SLEEP}(1)\n", [2]),
        (f"from time import {_SLEEP}\n{_SLEEP}(1)\n", [2]),
        (f"from time import {_SLEEP} as nap\nnap(1)\n", [2]),
        (f"import time\ndef f(s=time.{_SLEEP}):\n    s(1)\n", [2]),
        ("import time\ntime.monotonic()\n", []),
        (f"def f(clock):\n    clock.{_SLEEP}(1)\n", []),  # injected clock: fine
        (f"import asyncio\nasyncio.{_SLEEP}(1)\n", []),
    ],
)
def test_detector(source: str, expected: list[int]) -> None:
    assert direct_sleep_calls(source) == expected


def test_timing_import_contract_names_every_other_core_package() -> None:
    """The import-linter contract keeps `astrotool_core.timing` a leaf; it lists
    the forbidden core packages explicitly, so a new package must be added."""
    import tomllib

    config = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = config["tool"]["importlinter"]["contracts"]
    (contract,) = [c for c in contracts if c["source_modules"] == ["astrotool_core.timing"]]
    core = _REPO_ROOT / "packages" / "astrotool_core"
    packages = {
        f"astrotool_core.{path.parent.name}"
        for path in core.glob("*/__init__.py")
        if path.parent.name != "timing"
    }
    missing = sorted(packages - set(contract["forbidden_modules"]))
    assert not missing, f"add to the timing import contract in pyproject.toml: {missing}"
