"""Shared fixtures. Forces the Qt offscreen platform before any PySide6
import, so UI tests run headless on Windows/CI without a display.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
