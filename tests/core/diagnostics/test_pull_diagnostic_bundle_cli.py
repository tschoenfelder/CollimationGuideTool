"""``scripts/pull_diagnostic_bundle.py`` — the CLI front for
``astrotool_core.diagnostics.remote.pull_bundle`` (issue #6 reliability).

The transfer logic itself is covered by ``test_remote.py``; here we only pin the
CLI contract: argument wiring, exit codes, and that a resolution failure is
reported rather than raising a traceback.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "pull_diagnostic_bundle.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pull_diagnostic_bundle", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pdb = _load()

_UUID = "af7d27b7-7216-4421-a993-a63cbd111822"


def _fake_runner(
    listing: list[str],
) -> Callable[[list[str]], subprocess.CompletedProcess[str]]:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "ssh":
            return subprocess.CompletedProcess(
                args, 0, stdout="\n".join(listing) + "\n", stderr=""
            )
        remote_spec, dest = args[-2], args[-1]
        name = remote_spec.rsplit("/", 1)[-1]
        (Path(dest) / name).mkdir(parents=True, exist_ok=True)
        (Path(dest) / name / "incident.json").write_text("{}", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    return run


def test_successful_pull_returns_zero_and_prints_local_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = pdb.main(
        ["--uuid", "af7d27b7", "--local-dir", str(tmp_path)],
        runner=_fake_runner([_UUID]),
    )

    assert code == 0
    assert str(tmp_path / _UUID) in capsys.readouterr().out


def test_host_flag_is_passed_through(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(args)
        return _fake_runner([_UUID])(args)

    pdb.main(
        ["--uuid", _UUID, "--local-dir", str(tmp_path), "--host", "myrig"],
        runner=run,
    )

    assert seen[0][:2] == ["ssh", "myrig"]


def test_unresolvable_uuid_returns_nonzero_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = pdb.main(
        ["--uuid", "deadbeef", "--local-dir", str(tmp_path)],
        runner=_fake_runner([_UUID]),
    )

    assert code != 0
    assert "deadbeef" in capsys.readouterr().err
