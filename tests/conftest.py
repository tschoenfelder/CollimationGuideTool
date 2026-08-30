"""Shared fixtures. Forces the Qt offscreen platform before any PySide6
import, so UI tests run headless on Windows/CI without a display.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

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


@pytest.fixture(scope="session")
def qapp() -> Iterator[object]:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
