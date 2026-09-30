"""Test-tier auto-assignment in tests/conftest.py (issue #50): every
collected test gets exactly one tier marker from its path, `hardware` is
skipped unless opted in, and no test file falls outside the rules."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_TESTS_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT = _TESTS_DIR.parent


def _load_conftest() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_tiers_conftest", _TESTS_DIR / "conftest.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tiers = _load_conftest()


@pytest.mark.parametrize(
    ("relpath", "name", "expected"),
    [
        ("tests/collimation/domain/test_x.py", "test_a", "unit"),
        ("tests/guide/domain/test_x.py", "test_a", "unit"),
        ("tests/collimation/application/test_x.py", "test_a", "component"),
        ("tests/guide/application/test_x.py", "test_a", "component"),
        ("tests/collimation/ui/test_x.py", "test_a", "component"),
        ("tests/guide/ui/test_x.py", "test_a", "component"),
        ("tests/collimation/test_onstep_boundary.py", "test_a", "contract"),
        ("tests/contracts/test_camera_contract.py", "test_connect", "contract"),
        ("tests/contracts/test_camera_contract.py", "test_real_touptek_connect", "hardware"),
        ("tests/integration/test_x.py", "test_a", "integration"),
        ("tests/regressions/test_x.py", "test_a", "integration"),
        ("tests/local_data/test_x.py", "test_a", "integration"),
        ("tests/acceptance/test_x.py", "test_a", "acceptance"),
        ("tests/core/frames/test_frame.py", "test_a", "unit"),
        ("tests/core/target/test_new_module.py", "test_a", "unit"),
        ("tests/core/acquisition/test_stream_controller.py", "test_a", "component"),
        ("tests/core/onstep/test_onstep_adapters.py", "test_a", "component"),
    ],
)
def test_tier_for_maps_paths_to_one_tier(relpath: str, name: str, expected: str) -> None:
    assert tiers.tier_for(relpath, name) == expected


def test_a_path_outside_every_rule_has_no_tier() -> None:
    assert tiers.tier_for("tests/somewhere_new/test_x.py", "test_a") is None
    assert tiers.tier_for("tests/collimation/test_other_root_module.py", "test_a") is None


def test_every_test_file_in_the_repo_has_a_tier() -> None:
    untiered = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in _TESTS_DIR.rglob("test_*.py")
        if tiers.tier_for(path.relative_to(_REPO_ROOT).as_posix(), "test_a") is None
    ]
    assert untiered == []


def test_every_core_component_entry_names_a_real_module() -> None:
    missing = [m for m in tiers.CORE_COMPONENT_MODULES if not (_TESTS_DIR / "core" / m).is_file()]
    assert missing == []


_NOT_UNIT = re.compile(
    r"^\s*(import|from)\s+(threading|socket|subprocess|PySide6|concurrent)\b"
    r"|time\.sleep\(|Thread\(",
    re.MULTILINE,
)


def test_core_modules_using_threads_qt_sockets_or_sleep_are_not_unit() -> None:
    core = _TESTS_DIR / "core"
    misfiled = [
        path.relative_to(core).as_posix()
        for path in core.rglob("test_*.py")
        if path.name != Path(__file__).name
        and _NOT_UNIT.search(path.read_text(encoding="utf-8"))
        and path.relative_to(core).as_posix() not in tiers.CORE_COMPONENT_MODULES
    ]
    assert misfiled == []


def test_hardware_needs_an_explicit_opt_in() -> None:
    assert not tiers.hardware_opted_in(False, {})
    assert not tiers.hardware_opted_in(False, {tiers.HARDWARE_ENV_VAR: "0"})
    assert tiers.hardware_opted_in(True, {})
    assert tiers.hardware_opted_in(False, {tiers.HARDWARE_ENV_VAR: "1"})


def test_every_collected_test_in_this_session_has_exactly_one_tier(
    request: pytest.FixtureRequest,
) -> None:
    wrong = [
        item.nodeid
        for item in request.session.items
        if len({m.name for m in item.iter_markers() if m.name in tiers.TIERS}) != 1
    ]
    assert wrong == []


# --- the collection hook itself, against minimal stand-ins -----------------


class _Mark:
    def __init__(self, name: str) -> None:
        self.name = name


class _Item:
    def __init__(self, relpath: str, name: str, marks: tuple[str, ...] = ()) -> None:
        self.path = _REPO_ROOT / relpath
        self.nodeid = f"{relpath}::{name}"
        self.name = name
        self.originalname = name
        self.marks = [_Mark(m) for m in marks]

    def iter_markers(self) -> list[_Mark]:
        return list(self.marks)

    def add_marker(self, marker: Any) -> None:  # noqa: ANN401 - pytest MarkDecorator
        self.marks.append(_Mark(marker.name))

    def names(self) -> list[str]:
        return [m.name for m in self.marks]


class _Config:
    def __init__(self, run_hardware: bool = False) -> None:
        self.rootpath = _REPO_ROOT
        self._run_hardware = run_hardware

    def getoption(self, name: str) -> bool:
        assert name == "--run-hardware"
        return self._run_hardware


def _run_hook(items: list[_Item], *, run_hardware: bool = False) -> None:
    tiers.pytest_collection_modifyitems(_Config(run_hardware), items)


def test_hook_adds_the_path_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tiers.HARDWARE_ENV_VAR, raising=False)
    item = _Item("tests/guide/domain/test_x.py", "test_a")
    _run_hook([item])
    assert item.names() == ["unit"]


def test_hook_keeps_an_explicit_tier_marker_instead_of_the_path_tier() -> None:
    item = _Item("tests/guide/domain/test_x.py", "test_a", marks=("integration",))
    _run_hook([item])
    assert item.names() == ["integration"]


def test_hook_rejects_two_tier_markers() -> None:
    item = _Item("tests/guide/domain/test_x.py", "test_a", marks=("unit", "integration"))
    with pytest.raises(pytest.UsageError, match="more than one tier"):
        _run_hook([item])


def test_hook_rejects_a_test_with_no_tier() -> None:
    with pytest.raises(pytest.UsageError, match="no test tier"):
        _run_hook([_Item("tests/somewhere_new/test_x.py", "test_a")])


def test_hook_skips_hardware_unless_opted_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(tiers.HARDWARE_ENV_VAR, raising=False)
    skipped = _Item("tests/contracts/test_mount_contract.py", "test_real_onstep_mount")
    _run_hook([skipped])
    assert skipped.names() == ["hardware", "skip"]

    opted_in = _Item("tests/contracts/test_mount_contract.py", "test_real_onstep_mount")
    _run_hook([opted_in], run_hardware=True)
    assert opted_in.names() == ["hardware"]
