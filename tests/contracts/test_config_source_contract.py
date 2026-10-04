"""Config-source contract (#55 D01-D03, CONTRIBUTING.md "Duplicated knowledge").

The rig's EFW device name, the INDI endpoint (host and port 7624) and the config
file locations each have ONE owner:

- `astrotool_core/config/device_defaults.py` -- EFW device name, INDI host/port;
- `astrotool_core/config/paths.py` -- `~/.CollimationGuideTool`, `~/.SmartTScope`,
  `config.toml`.

This scans every production module and dev script (packages/, apps/, scripts/)
with the AST and fails if one of those literals is re-stated anywhere else -- a
second copy is how the wrong "ToupTek EFW 1" shipped (#47). Strings are searched,
not compared: the port as an int/float literal or as a standalone number inside
any string ("localhost:7624", URLs), either host anywhere in a string, any
"EFW <digit>" and any "ToupTek EFW" prefix (so f-strings and concatenations are
caught), and the config path parts. Docstrings are prose, not knowledge, and
are skipped; comments are not in the AST. Exceptions are pinned below with a
reason, and an exception that no longer matches anything fails too.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from astrotool_core.config import device_defaults, paths

_REPO = Path(__file__).resolve().parents[2]
_PRODUCTION_ROOTS = (_REPO / "packages", _REPO / "apps", _REPO / "scripts")

_OWNERS = frozenset(
    {
        "packages/astrotool_core/config/device_defaults.py",
        "packages/astrotool_core/config/paths.py",
    }
)

#: (module, literal) -> why it may stay. Keep this short.
_ALLOWED: dict[tuple[str, object], str] = {
    # D09 (audit: superficial): a test double mirroring a full OnStepAdapter
    # IndiRuntimeConfig. S6.0d was changing this file while S6.1 ran, so it was
    # left alone; tracker item "S6.1 follow-up: fake_onstep_indi_client.py adopts
    # device_defaults after S6.0d lands" (docs/restructuring-tasks.md). Delete
    # both entries then -- test_every_exception_is_still_needed enforces it.
    ("packages/astrotool_core/testing/fake_onstep_indi_client.py", "127.0.0.1"): "D09 test double",
    ("packages/astrotool_core/testing/fake_onstep_indi_client.py", 7624): "D09 test double",
}

_PORT = 7624
_EFW = "EFW <n>"
#: (pattern, the owned literal it re-states) -- searched inside every string.
_STRING_PATTERNS: tuple[tuple[re.Pattern[str], object], ...] = (
    (re.compile(r"(?<![\w.])7624(?!\w)"), _PORT),
    (re.compile(r"\blocalhost\b", re.IGNORECASE), "localhost"),
    (re.compile(r"(?<![\d.])127\.0\.0\.1(?!\d)"), "127.0.0.1"),
    (re.compile(r"\bEFW\s*\d"), _EFW),
    (re.compile(r"ToupTek\s+EFW"), _EFW),
    (re.compile(r"(?<!\w)\.CollimationGuideTool\b"), ".CollimationGuideTool"),
    (re.compile(r"(?<!\w)\.SmartTScope\b"), ".SmartTScope"),
    (re.compile(r"(?<![\w.])config\.toml\b"), "config.toml"),
)


def owned_in(value: object) -> list[object]:
    """Every owned literal the constant `value` re-states (de-duplicated, in order)."""
    if isinstance(value, bool):
        return []
    if isinstance(value, int | float):
        return [_PORT] if value == _PORT else []
    if not isinstance(value, str):
        return []
    hits: list[object] = []
    for pattern, literal in _STRING_PATTERNS:
        if pattern.search(value) and literal not in hits:
            hits.append(literal)
    return hits


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def owned_literals(source: str) -> Iterator[tuple[int, object]]:
    """`(line, literal)` for every owned literal in `source` outside docstrings."""
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in docstrings:
            for hit in owned_in(node.value):
                yield node.lineno, hit


def _production_modules() -> Iterator[tuple[str, Path]]:
    for root in _PRODUCTION_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(_REPO).as_posix(), path


def _findings() -> list[tuple[str, int, object]]:
    found = []
    for rel, path in _production_modules():
        if rel in _OWNERS:
            continue
        for line, literal in owned_literals(path.read_text(encoding="utf-8")):
            found.append((rel, line, literal))
    return found


def test_no_owned_literal_is_restated_outside_its_owner() -> None:
    offenders = [
        f"{rel}:{line}: {literal!r}"
        for rel, line, literal in _findings()
        if (rel, literal) not in _ALLOWED
    ]
    assert offenders == [], (
        "re-stated device default / config location -- import it from "
        "astrotool_core.config.device_defaults or astrotool_core.config.paths:\n"
        + "\n".join(offenders)
    )


def test_every_exception_is_still_needed() -> None:
    used = {(rel, literal) for rel, _, literal in _findings()}
    stale = [key for key in _ALLOWED if key not in used]
    assert stale == []


def test_the_owners_hold_the_literals() -> None:
    """Guards against the scan passing vacuously (e.g. an owner moved)."""
    for rel in _OWNERS:
        assert (_REPO / rel).is_file(), rel
    assert owned_in(device_defaults.EFW_DEVICE_NAME) == [_EFW]
    assert owned_in(device_defaults.INDI_HOST) == ["127.0.0.1"]
    assert owned_in(device_defaults.INDI_PORT) == [7624]
    assert owned_in(paths.APP_DIR_NAME) == [".CollimationGuideTool"]
    assert owned_in(paths.SMARTTSCOPE_DIR_NAME) == [".SmartTScope"]
    assert owned_in(paths.CONFIG_FILE_NAME) == ["config.toml"]
    assert owned_in(str(paths.DEFAULT_OWN_CONFIG_PATH)) == [".CollimationGuideTool", "config.toml"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("localhost:7624", [7624, "localhost"]),
        ("http://127.0.0.1:7624", [7624, "127.0.0.1"]),
        ("7624", [7624]),
        (7624.0, [7624]),
        ("--port=7624", [7624]),
        ("EFW 2", [_EFW]),
        ("efw: EFW1", [_EFW]),
        ("ToupTek EFW ", [_EFW]),  # the constant part of f"ToupTek EFW {n}"
        ("LOCALHOST", ["localhost"]),
        ("~/.CollimationGuideTool/diagnostics", [".CollimationGuideTool"]),
        # not owned:
        ("76240", []),
        ("17624", []),
        ("1.7624", []),
        ("127.0.0.10", []),
        ("my_localhost_alias", []),
        ("pyproject.toml", []),
        ("myconfig.toml", []),
        (True, []),
        (7625, []),
    ],
)
def test_the_matcher(value: object, expected: list[object]) -> None:
    assert owned_in(value) == expected


def test_the_scanner_catches_fstrings_and_concatenation() -> None:
    source = '''
N = 2
NAME = f"ToupTek EFW {N}"
SPLIT = "ToupTek " + "EFW 2"
URL = f"http://{HOST}:7624/"
'''
    assert sorted(owned_literals(source), key=str) == sorted(
        [(3, _EFW), (4, _EFW), (5, 7624)], key=str
    )


def test_the_scanner_catches_each_kind_of_restatement() -> None:
    source = '''
"""Docstring mentioning ToupTek EFW 1 and ~/.SmartTScope/config.toml is fine."""
NAME = "ToupTek EFW 1"
PORT = 7624
HOST = "localhost"
LOOPBACK = "127.0.0.1"
PATH = Path.home() / ".CollimationGuideTool" / "config.toml"
REMOTE = "~/.SmartTScope/config.toml"
FLAG = True
OTHER_PORT = 7625


def f():
    """Function docstring: localhost 7624."""
    return "ToupTek EFW 2"
'''
    assert sorted(owned_literals(source), key=str) == sorted(
        [
            (3, _EFW),
            (4, 7624),
            (5, "localhost"),
            (6, "127.0.0.1"),
            (7, ".CollimationGuideTool"),
            (7, "config.toml"),
            (8, ".SmartTScope"),
            (8, "config.toml"),
            (15, _EFW),
        ],
        key=str,
    )
