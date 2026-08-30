"""Shared fixtures. Forces the Qt offscreen platform before any PySide6
import, so UI tests run headless on Windows/CI without a display.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Cap BLAS/OpenMP's own internal thread pools to 1, set here (before any
# test module — and therefore numpy — gets imported, since a native
# thread pool reads these at first use) rather than per-test. Found
# investigating two segfaults that looked unrelated on the surface: a
# non-deterministic "Windows fatal exception: access violation" on a
# local dev machine, and later a 100%-reproducible SIGSEGV on Linux CI
# (tests/guide/application/test_guide_controller.py, deep inside
# smarttscope_live_analysis's np.count_nonzero, in a background thread
# started by StreamController/GuideController — code this change never
# touched). Both appeared only after this suite grew several tests that
# spawn real background threads doing heavy numpy FFT work
# (FovCalibrator/fov_registration) — consistent with numpy's/OpenBLAS's
# own internal thread pool being destabilized by many overlapping
# multi-threaded calls within one process, then crashing a *later*,
# unrelated test that also happens to do concurrent numpy work from a
# background thread. Single-threaded BLAS costs a little speed on this
# codebase's small array sizes, not correctness.
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")


@pytest.fixture(autouse=True)
def _isolate_camera_settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect CollimationTool MainWindow's default camera-settings file
    to a per-test tmp_path instead of the real
    ``~/.CollimationGuideTool/config.toml``.

    Without this, every test that constructs a bare `MainWindow(...)`
    (the vast majority — `camera_settings_path` is rarely passed
    explicitly) would read and overwrite the developer's/Pi's actual
    saved camera settings on every test run. Patches the name as
    imported into `main_window` (not the defining module) — see that
    constructor's own comment on why a bare global reference, not a
    default-parameter value, makes this patch effective.
    """
    try:
        import collimation_tool.ui.main_window as _main_window_module
    except ImportError:
        return
    monkeypatch.setattr(_main_window_module, "DEFAULT_CONFIG_PATH", tmp_path / "config.toml")


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def _flush_qt_events_after_each_test() -> Iterator[None]:
    """Process any pending Qt events after every test, not just the one
    test in this whole suite that happens to call processEvents() itself.

    Investigating a segfault (Windows access violation, later also a
    Linux SIGSEGV) that reproduced at that one call: hundreds of other
    tests create and destroy QWidgets/QPixmaps/QTimers via plain Python
    refcounting without ever running the Qt event loop, so any
    deleteLater()-deferred cleanup Qt itself queues along the way never
    gets flushed — it just accumulates for the entire session until the
    first processEvents() call has to process all of it at once, by
    which point some of it may reference memory Python's own GC already
    freed. Flushing incrementally after each test keeps that backlog
    from ever building up. A no-op for tests that never touch Qt (no
    QApplication instance exists yet, so there's nothing to flush).
    """
    yield
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            app.processEvents()
    except ImportError:
        pass
