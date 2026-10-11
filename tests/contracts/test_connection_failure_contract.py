"""Connection-failure contract (#52 K01/K02, CONTRIBUTING.md "Duplicated knowledge").

How a device connect/disconnect failure is caught, worded and logged has ONE owner:
`astrotool_core/session/device_connection.py` (`LIFECYCLE_FAILURES`/`INTERNAL_ERRORS`,
`ConnectionFailure.status_text`, the log record in `run_phase`). Before S6.2 each panel and the
GuideTool wrote its own copy, and the copies disagreed (GuideTool caught only ConnectionError;
FilterWheelPanel never logged).

This scans production modules with the AST and fails on

- a re-stated failure wording ("connect failed", "connection failed", "disconnect failed",
  "could not connect", "unable to connect") in any string of the apps (f-string parts
  included; docstrings are prose and skipped). `packages/` is not scanned for wording: there
  these phrases are the adapters' own exception messages (the device's reason, e.g.
  "IndiClient: could not connect to indiserver ..."), which the owner then reports -- not a
  second reporting of the failure;
- a `try` in `apps/` or `packages/` that calls `.connect()` and whose handlers do not cover
  `Exception` (e.g. `except ConnectionError` or `except (ConnectionError, TimeoutError)`) --
  the narrow-set bug class of 7bb7b68 (a TimeoutError crashed Connect) and the GuideTool (K01).

Exceptions are pinned with a reason, and an exception that no longer matches fails too.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from astrotool_core.session import device_connection

_REPO = Path(__file__).resolve().parents[2]
_OWNER = Path(device_connection.__file__).resolve().relative_to(_REPO).as_posix()
_WORDING = re.compile(
    r"\b(?:dis)?connect(?:ion)? failed\b|\b(?:could not|unable to|cannot) (?:dis)?connect\b",
    re.IGNORECASE,
)
_COVERING = {"Exception", "BaseException"}

#: module -> why it may still restate the wording. Keep this short.
_ALLOWED_WORDING: dict[str, str] = {
    "apps/collimation_tool/ui/mount_test_move_panel.py": (
        "S6.2 contract excludes this panel (S6.6/S6.7 own it): its connect handler still has its "
        "own copy; it moves to DeviceConnectionService with the Mount Align orchestration (S6.6)"
    ),
}

#: (module, line of the `try`) -> why a connect() there may catch less than Exception.
_ALLOWED_NARROW: dict[tuple[str, int], str] = {}


def _modules(*roots: str) -> list[Path]:
    return sorted(
        path
        for root in roots
        for path in (_REPO / root).rglob("*.py")
        if "__pycache__" not in path.parts
    )


def _docstring_nodes(tree: ast.Module) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _wording_hits(source: str) -> Iterator[int]:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _WORDING.search(node.value)
        ):
            yield node.lineno


def _calls_connect(body: list[ast.stmt]) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "connect"
                and not node.args
            ):
                return True
    return False


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return {"BaseException"}  # bare `except:`
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {ast.unparse(t).rsplit(".", 1)[-1] for t in types}


def _narrow_connect_handlers(source: str) -> Iterator[int]:
    """Lines of each `try` around a `.connect()` whose handlers do not cover `Exception`."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Try) and node.handlers and _calls_connect(node.body):
            caught = set().union(*(_handler_names(h) for h in node.handlers))
            if not caught & _COVERING:
                yield node.lineno


def _rel(path: Path) -> str:
    return path.relative_to(_REPO).as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_owner_defines_the_wording() -> None:
    assert list(_wording_hits(_read(_REPO / _OWNER))), "the owner itself must hold the wording"


def test_no_app_module_restates_the_failure_wording() -> None:
    offenders = [
        f"{_rel(path)}:{line}"
        for path in _modules("apps")
        if _rel(path) not in _ALLOWED_WORDING
        for line in _wording_hits(_read(path))
    ]
    assert offenders == [], (
        "connect/disconnect failure wording re-stated outside "
        f"{_OWNER} -- use ConnectionFailure.status_text: {offenders}"
    )


@pytest.mark.parametrize("module", sorted(_ALLOWED_WORDING))
def test_every_wording_exception_is_still_needed(module: str) -> None:
    assert list(_wording_hits(_read(_REPO / module))), f"{module} no longer needs its exception"


def _narrow_offenders() -> list[tuple[str, int]]:
    return [
        (_rel(path), line)
        for path in _modules("apps", "packages")
        for line in _narrow_connect_handlers(_read(path))
    ]


def test_no_connect_is_guarded_by_a_narrow_exception_set() -> None:
    offenders = [hit for hit in _narrow_offenders() if hit not in _ALLOWED_NARROW]
    assert offenders == [], (
        "a connect() guarded by handlers that do not cover Exception -- OnStepAdapter/INDI/SDK "
        f"raise more (TimeoutError, KeyError, OSError, HRESULTException); use {_OWNER}: "
        f"{offenders}"
    )


def test_every_narrow_exception_is_still_needed() -> None:
    found = set(_narrow_offenders())
    stale = [hit for hit in _ALLOWED_NARROW if hit not in found]
    assert stale == [], f"remove these _ALLOWED_NARROW entries: {stale}"


class TestTheScannersThemselves:
    _GUIDETOOL_BEFORE_S62 = (
        "def f(candidate, label):\n"
        "    try:\n"
        "        candidate.connect()\n"
        "    except ConnectionError as exc:\n"
        "        label.setText(str(exc))\n"
    )

    @pytest.mark.parametrize(
        "handler",
        [
            "except ConnectionError as exc:",
            "except (ConnectionError, TimeoutError) as exc:",
            "except (ConnectionError, RuntimeError, TimeoutError, ValueError) as exc:",
            "except socket.error as exc:",
        ],
    )
    def test_a_narrow_handler_set_is_found(self, handler: str) -> None:
        source = self._GUIDETOOL_BEFORE_S62.replace("except ConnectionError as exc:", handler)
        assert list(_narrow_connect_handlers(source)) == [2]

    @pytest.mark.parametrize(
        "handler",
        [
            "except Exception as exc:",
            "except (ConnectionError, Exception) as exc:",
            "except BaseException as exc:",
            "except:",
            "except builtins.Exception as exc:",
        ],
    )
    def test_a_handler_covering_exception_is_accepted(self, handler: str) -> None:
        source = self._GUIDETOOL_BEFORE_S62.replace("except ConnectionError as exc:", handler)
        assert list(_narrow_connect_handlers(source)) == []

    def test_try_finally_and_qt_signal_connects_are_not_connect_guards(self) -> None:
        source = (
            "def f(device, button, slot):\n"
            "    try:\n"
            "        device.connect()\n"
            "    finally:\n"
            "        pass\n"
            "    try:\n"
            "        button.clicked.connect(slot)\n"
            "    except ValueError:\n"
            "        pass\n"
        )
        assert list(_narrow_connect_handlers(source)) == []

    @pytest.mark.parametrize(
        "text",
        [
            "Connect failed — {exc}",
            "MountTestMovePanel: connect failed: %s",
            "Disconnect failed",
            "Connection failed: refused",
            "Could not connect to the mount",
            "unable to connect",
        ],
    )
    def test_the_wording_scan_finds_variants(self, text: str) -> None:
        assert list(_wording_hits(f"label = {text!r}\n")) == [1]
        assert list(_wording_hits(f"label = f'x {{a}} {text}'\n")) == [1]

    def test_docstrings_are_prose(self) -> None:
        assert list(_wording_hits('def f():\n    """Connect failed is shown."""\n')) == []
