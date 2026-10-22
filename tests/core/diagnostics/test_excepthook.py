"""#55 K04 (S6.9): the one unhandled-exception boundary both apps install."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest
from astrotool_core.diagnostics.excepthook import install_excepthook

pytestmark = pytest.mark.unit


class _Label:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, text: str) -> None:  # noqa: N802 -- the Qt name
        self.text = text


def _hooked(
    monkeypatch: pytest.MonkeyPatch, bundle: object, window: object
) -> tuple[list[type[BaseException]], list[BaseException]]:
    previous: list[type[BaseException]] = []

    def previous_hook(t: type[BaseException], _v: BaseException, _tb: TracebackType | None) -> None:
        previous.append(t)

    captured: list[BaseException] = []

    def capture(exc: BaseException) -> object:
        captured.append(exc)
        return bundle

    monkeypatch.setattr(sys, "excepthook", previous_hook)
    diagnostics: Any = SimpleNamespace(capture_exception=capture)
    install_excepthook(diagnostics, active_window=lambda: window)
    return previous, captured


def test_a_captured_incident_is_shown_and_the_previous_hook_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    label = _Label()
    bundle = SimpleNamespace(incident_id="abc-123", path=Path("x"))
    previous, captured = _hooked(
        monkeypatch, bundle, SimpleNamespace(_diagnostics_status_label=label)
    )
    error = RuntimeError("boom")
    sys.excepthook(RuntimeError, error, None)
    assert captured == [error]
    assert label.text == "abc-123"
    assert previous == [RuntimeError]  # the traceback is never hidden


def test_no_bundle_or_no_window_still_reaches_the_previous_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous, _ = _hooked(monkeypatch, None, None)
    sys.excepthook(ValueError, ValueError("x"), None)
    previous2, _ = _hooked(monkeypatch, SimpleNamespace(incident_id="i", path=Path("p")), None)
    sys.excepthook(KeyError, KeyError("k"), None)
    assert previous == [ValueError]
    assert previous2 == [KeyError]


def test_both_apps_install_the_shared_helper() -> None:
    """No app keeps its own copy of the hook body (the K04 duplicate)."""
    repo = Path(__file__).resolve().parents[3]
    for app in ("collimation_tool", "guide_tool"):
        source = (repo / "apps" / app / "main.py").read_text(encoding="utf-8")
        assert "install_excepthook(diagnostics, active_window=" in source
        assert "capture_exception" not in source
