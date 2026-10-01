"""Shared fixtures. Forces the Qt offscreen platform before any PySide6
import, so UI tests run headless on Windows/CI without a display.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
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


@pytest.fixture(autouse=True)
def _isolate_pixel_scale_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """MainWindow reads each optical train's plate scale from the real
    ``~/.SmartTScope/config.toml`` unless a test passes one explicitly. Without
    this, a developer/Pi machine that HAS that file would silently switch
    mount calibration (issue #46) from the fixed pulse to sized moves in every
    bare `MainWindow(...)` test. Tests that need a scale pass it explicitly."""
    try:
        import collimation_tool.ui.main_window as _main_window_module
    except ImportError:
        return
    monkeypatch.setattr(_main_window_module, "load_pixel_scale_arcsec", lambda _train: None)


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


# ---------------------------------------------------------------------------
# Test tiers (issue #50). Every collected test gets exactly one tier marker,
# assigned from its path by `tier_for` below (an explicit tier marker on the
# test itself wins). CONTRIBUTING.md "Test pyramid..." describes the tiers;
# `scripts/check.ps1` / `check.sh` and CI select by them.
# ---------------------------------------------------------------------------

TIERS = ("unit", "component", "contract", "integration", "acceptance", "hardware")

# tests/core is `unit` by default. A tests/core module is `component` when it
# drives a production component through a fake boundary (Fake* adapter, fake
# SDK/runner/INDI server, FakeClock) or uses threads, sockets, subprocesses or
# real sleeps. tests/core/testing/test_test_tiers.py enforces the mechanical
# part (threading/socket/subprocess/PySide6/sleep => listed here).
CORE_COMPONENT_MODULES = frozenset(
    {
        "acquisition/test_acquisition_fake_time.py",
        "acquisition/test_motion_aware_acquisition.py",
        "acquisition/test_motion_aware_acquisition_simulated.py",
        "acquisition/test_single_capture.py",
        "acquisition/test_stable_frame_acquisition.py",
        "acquisition/test_stable_frame_acquisition_simulated.py",
        "acquisition/test_stream_controller.py",
        "camera/test_touptek_adapter_identity.py",
        "camera/test_touptek_adapter_no_hardware.py",
        "camera/test_touptek_adapter_simulated.py",
        "diagnostics/test_pull_diagnostic_bundle_cli.py",
        "diagnostics/test_remote.py",
        "filter_wheel/test_indi_filter_wheel_adapter.py",
        "filter_wheel/test_indi_filter_wheel_adapter_simulated.py",
        "filter_wheel/test_registry.py",
        "indi/test_indi_client.py",
        "mount/test_axis_calibration.py",
        "mount/test_operating_mode.py",
        "mount/test_tracking_mode.py",
        "onstep/test_onstep_adapters.py",
        "onstep/test_onstep_simulator.py",
        "registration/test_astap_adapter.py",
        "registration/test_star_field_registrar.py",
        "testing/test_changed_tests.py",
        "timing/test_deadline.py",
        "timing/test_fake_clock.py",
    }
)

# First matching prefix wins (after the tests/core and hardware rules).
_PREFIX_TIERS = (
    ("tests/contracts/", "contract"),
    # architectural guard: no OnStep access outside OnStepAdapter
    ("tests/collimation/test_onstep_boundary.py", "contract"),
    ("tests/integration/", "integration"),
    ("tests/regressions/", "integration"),
    # real captured frames, local-only (skipped when the dataset is absent);
    # replays real data through several algorithms, no device involved
    ("tests/local_data/", "integration"),
    ("tests/acceptance/", "acceptance"),
    ("tests/collimation/domain/", "unit"),
    ("tests/guide/domain/", "unit"),
    ("tests/collimation/application/", "component"),
    ("tests/guide/application/", "component"),
    ("tests/collimation/ui/", "component"),
    ("tests/guide/ui/", "component"),
)

HARDWARE_ENV_VAR = "ASTROTOOL_RUN_HARDWARE"


def tier_for(relpath: str, test_name: str) -> str | None:
    """Tier of a test from its repo-relative posix path and function name;
    None when the path isn't covered by any rule (a collection error)."""
    if relpath.startswith("tests/contracts/") and test_name.startswith("test_real_"):
        return "hardware"  # real-device contract cases (skipif-guarded too)
    if relpath.startswith("tests/core/"):
        module = relpath.removeprefix("tests/core/")
        return "component" if module in CORE_COMPONENT_MODULES else "unit"
    for prefix, tier in _PREFIX_TIERS:
        if relpath.startswith(prefix):
            return tier
    return None


def hardware_opted_in(run_hardware_flag: bool, environ: Mapping[str, str]) -> bool:
    return run_hardware_flag or environ.get(HARDWARE_ENV_VAR) == "1"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-hardware",
        action="store_true",
        default=False,
        help=f"run `hardware`-tier tests (real devices); or set {HARDWARE_ENV_VAR}=1",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_hardware = hardware_opted_in(bool(config.getoption("--run-hardware")), os.environ)
    skip_hardware = pytest.mark.skip(
        reason=f"hardware tier: opt in with --run-hardware or {HARDWARE_ENV_VAR}=1"
    )
    for item in items:
        explicit = {mark.name for mark in item.iter_markers() if mark.name in TIERS}
        if len(explicit) > 1:
            raise pytest.UsageError(f"{item.nodeid}: more than one tier marker {sorted(explicit)}")
        if explicit:
            tier: str | None = explicit.pop()
        else:
            relpath = item.path.resolve().relative_to(config.rootpath.resolve()).as_posix()
            tier = tier_for(relpath, getattr(item, "originalname", item.name))
            if tier is None:
                raise pytest.UsageError(
                    f"{item.nodeid}: no test tier for this path -- add a rule to "
                    "tier_for() in tests/conftest.py"
                )
            item.add_marker(getattr(pytest.mark, tier))
        if tier == "hardware" and not run_hardware:
            item.add_marker(skip_hardware)
