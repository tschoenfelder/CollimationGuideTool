# CollimationGuideTool: astrotool_core + CollimationTool + GuideTool — Implementation Plan

## Context

`C:\Users\tscho\Documents\Torsten\TSBrain\CollimationGuideTool` currently contains exactly one file — `collimation-guidetool-architektur.md`, a German architecture requirements doc — and nothing else: no code, no `pyproject.toml`, not yet its own git repository (it's an untracked subdirectory of the TSBrain repo). The doc specifies two PySide6 desktop apps, `CollimationTool` and `GuideTool`, sharing a UI-independent core library `astrotool_core` (camera, frames, target/ROI tracking, mount access, acquisition, session logging, testing infra), with a strong emphasis on regression protection: a strict one-way dependency rule, small public interfaces per subsystem, contract tests per adapter, characterization tests before touching working code, golden-master replay datasets with tolerance-based comparison, impact-scoped test suites, an explicit change-classification convention, no incidental refactoring, and a changed-file budget for AI-assisted patches.

The user explicitly authorized drawing on prior art from the sibling project `C:\Users\tscho\Documents\Torsten\TSBrain\smart_telescope` (a FastAPI+browser-UI app controlling a Celestron C8 + ToupTek camera + OnStep mount). Three exploration passes over that codebase, plus a GitHub survey of the user's account (`tschoenfelder`), turned up a large amount of directly reusable prior art — both inline in `smart_telescope` and in two separate, already-published, hardware-independent libraries (`OnStepAdapter`, `SmartTScopeLiveAnalysis`) that the user already maintains as standalone pip-installable packages. Three open architecture decisions were resolved with the user before finalizing this plan:

1. **Mount protocol**: `astrotool_core/mount/indi_adapter.py` wraps the existing `onstep-adapter` pip package (from `https://github.com/tschoenfelder/OnStepAdapter`, currently `v0.3.4`) over a real serial connection — the same proven path `smart_telescope` uses — rather than building a literal INDI/`pyindi-client`/`indiserver` stack. The file keeps the doc's name (`indi_adapter.py`) even though it doesn't speak the INDI wire protocol.
2. **Repository**: one dedicated new git repository at `CollimationGuideTool/` (not nested inside TSBrain, not split into per-component repos), matching the architecture doc's explicit "ein Repository" recommendation and its "one install, two menu entries" deployment model.
3. **Detection/tracking reuse**: `astrotool_core.target` takes a runtime pip dependency on `smarttscope-live-analysis` (from `https://github.com/tschoenfelder/SmartTScopeLiveAnalysis`) for multi-source star detection and multi-frame temporal track-linking, instead of reimplementing that logic. This library is already boundary-clean (no camera/mount/FITS coupling, NumPy-only) and already does most of what a shared `target` subsystem needs.

The intended outcome of this plan is a concrete, staged scaffolding + build order that a coding agent can execute directly, with the regression-protection machinery set up as real pytest/tooling config from the start rather than added later.

---

## Repository layout

Initialize `CollimationGuideTool/` as its own git repository. Final tree:

```
CollimationGuideTool/
├── collimation-guidetool-architektur.md      # existing — source of truth, unchanged
├── PLAN.md                                    # this file
├── README.md
├── CONTRIBUTING.md                            # change-classification + AI-patch-budget convention
├── .gitignore
├── pyproject.toml
├── packages/
│   └── astrotool_core/
│       ├── __init__.py
│       ├── camera/       (__init__.py, port.py, capabilities.py, touptek_adapter.py, replay_camera.py, fake_camera.py)
│       ├── frames/       (__init__.py, frame.py, pixel_format.py, analysis_plane.py, frame_buffer.py)
│       ├── target/       (__init__.py, point_source.py, detector.py, roi_selector.py, roi_tracker.py)
│       ├── mount/        (__init__.py, port.py, no_mount.py, indi_adapter.py, axis_calibration.py)
│       ├── focus/        (__init__.py, port.py, no_focuser.py, fake_focuser.py)   # addition vs. doc tree — see note below
│       ├── acquisition/  (__init__.py, single_capture.py, stream_controller.py, acquisition_state.py)
│       ├── session/      (__init__.py, session_context.py, event_log.py, frame_recorder.py)
│       └── testing/      (__init__.py, frame_factory.py, replay_dataset.py, fake_touptek.py, fake_mount.py)
├── apps/
│   ├── collimation_tool/
│   │   ├── __init__.py, main.py
│   │   ├── domain/       (collimation_measurement.py, symmetry_analysis.py, diffraction_analysis.py, focus_metric.py, collimation_state.py)
│   │   ├── application/  (collimation_controller.py, focus_controller.py, recenter_policy.py)
│   │   └── ui/           (main_window.py, collimation_view.py, focus_view.py, collimation_overlays.py)
│   └── guide_tool/
│       ├── __init__.py, main.py
│       ├── domain/       (guide_error.py, drift_estimator.py, correction_model.py, guiding_state.py)
│       ├── application/  (guide_controller.py, calibration_controller.py, correction_policy.py)
│       └── ui/           (main_window.py, guide_view.py, calibration_view.py, guide_overlays.py)
├── tests/
│   ├── conftest.py                # shared fixtures — Mock(spec=Port) style, mirrors smart_telescope's tests/conftest.py
│   ├── core/          (camera/, frames/, target/, mount/, focus/, acquisition/, session/ — mirrors packages/astrotool_core)
│   ├── collimation/    (domain/, application/, ui/ — mirrors apps/collimation_tool)
│   ├── guide/          (domain/, application/, ui/ — mirrors apps/guide_tool)
│   ├── contracts/      (test_camera_contract.py, test_mount_contract.py, test_focuser_contract.py)
│   └── integration/    (test_roi_tracker_replay.py, test_collimation_golden_master.py, test_guide_golden_master.py, test_cross_app_smoke.py, _golden_master.py helper)
└── datasets/
    ├── collimation/  (mono_centered/, mono_adjustment_shift/, color_bayer/, artificial_star/)
    └── guiding/      (steady_drift/, axis1_response/, axis2_response/, lost_star/)
```

Each dataset leaf: replayed FITS frames (or a synthetic-generator config) + `expected.json` (tolerance-checked numeric results) + `README.md` (provenance), mirroring `smart_telescope/tests/fixtures/README.md`'s auto-skip-if-missing convention.

**Note on `focus/`**: not in the architecture doc's literal tree, but `CollimationTool` needs focuser control (`focus_controller.py`) and `smart_telescope/ports/focuser.py` + its mock/simulator adapters already exist as a direct port source. Added for symmetry with `mount/` (port + no-op + fake) since focuser hardware is a real, separate device from the mount. Call this out in the first commit's message/README as a deliberate, documented addition to the doc's tree, not a silent deviation.

### Packaging

Single root `pyproject.toml`, one distribution, `packages/` + `apps/` discovered together — this matches the doc's own "one install, two menu entries" model and avoids needing `uv`/`hatch` workspace tooling (confirmed not installed; plain pip/setuptools is the working toolchain, per `TSBrain/pyproject.toml`).

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "astro-tools"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
    "numpy>=1.26",
    "astropy>=6.0",
    "PySide6>=6.7",
    "onstep-adapter @ https://github.com/tschoenfelder/OnStepAdapter/releases/download/v0.3.4/onstep_adapter-0.3.4-py3-none-any.whl",
    "smarttscope-live-analysis @ git+https://github.com/tschoenfelder/SmartTScopeLiveAnalysis.git@v0.1.0",
]

[project.optional-dependencies]
touptek = []   # add the vendored ToupTek SDK wheel path here once sourced, mirrors smart_telescope's resources/camera_adapter pattern
dev = [
    "pytest>=8.0", "pytest-cov>=5.0", "pytest-mock>=3.15",
    "ruff>=0.4", "mypy>=1.10", "import-linter>=2.0",
]

[project.scripts]
collimation-tool = "collimation_tool.main:main"
guide-tool = "guide_tool.main:main"

[tool.setuptools.packages.find]
where = ["packages", "apps"]
include = ["astrotool_core*", "collimation_tool*", "guide_tool*"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--cov=packages/astrotool_core --cov=apps/collimation_tool --cov=apps/guide_tool --cov-report=term-missing --cov-fail-under=80"

[tool.ruff]
line-length = 100
target-version = "py313"
[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "ANN"]

[tool.mypy]
python_version = "3.13"
strict = true
ignore_missing_imports = true

[tool.importlinter]
root_packages = ["astrotool_core", "collimation_tool", "guide_tool"]

[[tool.importlinter.contracts]]
name = "core never imports the apps"
type = "forbidden"
source_modules = ["astrotool_core"]
forbidden_modules = ["collimation_tool", "guide_tool"]

[[tool.importlinter.contracts]]
name = "adapters never import a UI toolkit"
type = "forbidden"
source_modules = ["astrotool_core.camera.touptek_adapter", "astrotool_core.mount.indi_adapter"]
forbidden_modules = ["PySide6"]

[[tool.importlinter.contracts]]
name = "roi_tracker never touches the mount"
type = "forbidden"
source_modules = ["astrotool_core.target.roi_tracker"]
forbidden_modules = ["astrotool_core.mount"]
```

`lint-imports` runs alongside `pytest` on every change — this turns the doc's "Verboten wären beispielsweise" examples (dependency-rule violations) into an actual gate, not just a code-review reminder.

---

## What ports directly vs. what's genuinely new

### Direct/near-direct ports from `smart_telescope` (adapt, don't redesign)

| Target file | Source | Notes |
|---|---|---|
| `camera/port.py`, `capabilities.py` | `smart_telescope/ports/camera.py`, `domain/camera_capabilities.py` | Trim to what both apps need |
| `camera/touptek_adapter.py` | `smart_telescope/adapters/touptek/managed.py` (`SmartTouptekCamera`) | Port the pixel-shift auto-detect heuristic (`_detect_pixel_shift`) as-is — write a characterization test pinning its current behavior on synthetic 12/14/16-bit MSB-aligned inputs *before* touching it (doc §4 discipline). Also port the `EnumV2()`-called-at-most-once-per-process guard (documented Pi/Python-3.13 crash history). |
| `camera/fake_camera.py`, `testing/fake_touptek.py` | `smart_telescope/adapters/mock/camera.py` (`MockCamera`) | Configurable fail_connect/fail_on_capture/bright-vs-dim frames/abortable delay |
| `camera/replay_camera.py` | `smart_telescope/adapters/replay/camera.py` (`ReplayCamera`, `ReplayCameraAdapter`) | FITS-directory and in-memory-array variants |
| `frames/frame.py` | `smart_telescope/domain/frame.py` (`FitsFrame`) + `domain/collimation/processing/frame.py` (`ProcessedFrame`/`normalize_frame`) | Merge into one frame type |
| `acquisition/stream_controller.py` | `smart_telescope/services/managed_camera.py` (`ManagedCamera`, `FrameMailbox`) | Background-thread capture + single-slot mailbox on top of single-shot `capture()` |
| `session/session_context.py`, `event_log.py` | `smart_telescope/services/section_logger.py`, `service_call_logger.py` | One `session_id = uuid4()` threaded through per-section loggers + structured JSON-line call records |
| `session/frame_recorder.py`, `testing/replay_dataset.py` | `smart_telescope/services/diagnostic_frame_store.py` + `adapters/replay/camera.py` | Writer/reader pair: standardized FITS headers + deterministic filename convention |
| `mount/port.py`, `no_mount.py` | `smart_telescope/ports/mount.py`, `adapters/mock/mount.py` (`MockMount`), `adapters/simulator/mount.py` (`SimulatorMount`) | Trim to the doc's minimal Protocol (connect/disconnect/capabilities/status/pulse_axis) — deliberately smaller than smart_telescope's full goto/park/align surface, since collimation/guiding only need pulsed axis correction |
| `mount/indi_adapter.py` | `smart_telescope/adapters/onstep/mount.py` (`OnStepMount` shim over `onstep_adapter.OnStepClient`) | Same shim discipline: only wrapper/glue + documented `SYNC-OVERRIDE` patches, never edit the `onstep_adapter` package internals — flag gaps in chat and wait, per this session's own established convention for that library |
| `focus/port.py`, `no_focuser.py`, `fake_focuser.py` | `smart_telescope/ports/focuser.py` + mock/simulator adapters | New subsystem addition (see note above) but the port itself already exists |
| Config loading | `smart_telescope/config.py` | Search-path priority (`~/.CollimationGuideTool/config.toml` → CWD → project root), `tomllib`, env-var-overrides-file-overrides-default layering, dataclass-per-TOML-table — same pattern, new search paths |

### New — target/detector.py and roi_tracker.py, built on `smarttscope-live-analysis`

- `target/point_source.py` — thin dataclass wrapping the library's per-source records (`x, y, peak, area, kind`).
- `target/detector.py` — calls `smarttscope_live_analysis.analysis.analyze_frame()` for single-frame detection and `smarttscope_live_analysis.clustering.suggest_capture_adjustments()` where relevant; adapts its `Source` objects to `point_source.PointSource`.
- `target/roi_tracker.py` — **the one piece with no full existing analog anywhere**, but now meaningfully smaller than originally scoped: `smarttscope_live_analysis.temporal.track_sources()` + `classify_temporal_tracks()` already do multi-frame nearest-neighbor linking and persistent/transient classification. What's still missing and must be written fresh is the *single-locked-target* state machine layer on top of that: `INITIALIZING → LOCKED ⇄ LOST → SEARCHING → REACQUIRED → LOCKED`, exposing `update(detector_result) -> TrackingResult` and never importing `astrotool_core.mount`. TDD this (no legacy behavior to characterize — it's genuinely new code).
- `target/roi_selector.py` — initial ROI/lock-point selection from a detector result (pick the brightest persistent-candidate source), still new but small.

This reuse decision meaningfully de-risks Stage 3 below versus a from-scratch temporal tracker.

### Heavy-port stages (confirmed present, large existing algorithm library)

`smart_telescope/domain/collimation/processing/*` (donut_detection.py, obstruction_detection.py, spike_detection.py, spike_decomposition.py, geometry_fits.py, stretch.py) and `smart_telescope/services/collimation/*` (collimation_advisor.py, fine_collimation_advisor.py, screw_mapper.py, focus_search.py, fwhm_focus.py, mount_centering.py::PulseCenterer, state_machine.py, assistant.py) → primary port source for `apps/collimation_tool/domain/` and `application/`.

`smart_telescope/domain/guiding.py`, `services/guide_measurement.py`, `services/guiding_service.py` → primary port source for `apps/guide_tool/domain/` and `application/`. Note `WouldGuidePulse` already separates *computing* a correction from *sending* it — exactly the doc's policy/adapter split.

### Genuinely new, no analog anywhere (confirmed after all exploration)

1. `target/roi_tracker.py`'s lock-state machine (scoped down per above, but still new).
2. `frames/pixel_format.py` — real mono/Bayer demosaic handling. Confirmed absent from both `smart_telescope` and `smarttscope-live-analysis`; the doc's own comparison table and dataset tree (`datasets/collimation/color_bayer/`) require it.
3. All of `ui/` in both apps — smart_telescope's UI is FastAPI+browser JS, zero PySide6 overlap.
4. `mount/axis_calibration.py`, `guide_tool/application/calibration_controller.py` — no dedicated pulse-then-measure-response calibration routine exists; write from the standard N/S/E/W-pulse pattern, leaning on `PulseCenterer`'s pulse-issuing shape for the send side and `roi_tracker` for the measure side.
5. `guide_tool/domain/drift_estimator.py` — check once more during Stage 0 inventory in case something closer already exists in `guide_measurement.py` before assuming new work.

---

## Staged build order

Each stage ends with something concretely runnable/testable.

**Stage 0 — Inventory, repo init, scaffolding**
Full read-through of the smart_telescope files listed above (write source→target path mapping notes); `git init` a fresh repo at `CollimationGuideTool/`; create the directory tree, `pyproject.toml`, `.gitignore`, `import-linter` config, empty `__init__.py` files. `pip install -e ".[dev]"` and confirm it installs cleanly (including the two new pinned dependencies) with zero packages of our own yet. First commit = scaffolding only.
*Done when:* `pytest tests/` collects zero tests but exits 0; `lint-imports` runs clean; `ruff check .` clean.

**Stage 1 — astrotool_core: frames + target (built on smarttscope-live-analysis), no camera/mount hardware**
Port `frames/frame.py`, `analysis_plane.py`. Write `frames/pixel_format.py` fresh, TDD-first against synthetic Bayer arrays. Build `target/point_source.py`, `detector.py` on top of `smarttscope_live_analysis.analysis`/`clustering`. Write `testing/frame_factory.py` (synthetic single/multi-star, hot-pixel, Bayer frame builders).
*Done when:* `pytest tests/core/frames tests/core/target -v` green; a synthetic single-Gaussian-star frame round-trips through `detector` → `PointSource` with centroid within tolerance.

**Stage 2 — astrotool_core: camera + mount + focus ports and fakes (still no real hardware)**
Port `camera/port.py`, `capabilities.py`, `fake_camera.py`, `testing/fake_touptek.py`. Port `mount/port.py` (doc's minimal Protocol), `no_mount.py`, `testing/fake_mount.py`. Port `focus/port.py`, `no_focuser.py`, `fake_focuser.py`. Write `tests/contracts/test_camera_contract.py` parametrized over `[fake_camera_factory]`, `test_mount_contract.py` over `[no_mount_factory, fake_mount_factory]`, `test_focuser_contract.py` similarly.
*Done when:* all three contract test files pass for their fake/no-op implementations.

**Stage 3 — astrotool_core: acquisition, session, real adapters, roi_tracker**
Port `acquisition/stream_controller.py`, `single_capture.py`, `acquisition_state.py`. Port `session/session_context.py`, `event_log.py`, `frame_recorder.py`. Port `camera/touptek_adapter.py` + `replay_camera.py` as a matched write/read pair — characterization-test the pixel-shift heuristic before adapting it. Port `mount/indi_adapter.py` wrapping `onstep_adapter.OnStepClient`/`OnStepMount`, same shim discipline as `smart_telescope`. Write `target/roi_tracker.py` and `roi_selector.py` fresh, TDD-first. Extend camera/mount contract tests to include `replay_camera_factory`/real-adapter factories (`skipif`-guarded, no hardware/fixtures present in this Windows dev environment).
*Done when:* a recorded replay sequence run through `stream_controller` → `detector` → `roi_tracker` reproduces the doc's own example transition sequence `[LOCKED, LOCKED, LOST, SEARCHING, REACQUIRED]` — first golden-master test, committed as `tests/integration/test_roi_tracker_replay.py` + `datasets/collimation/mono_adjustment_shift/`.

**Stage 4 — mount/axis_calibration.py**
New pulse-then-measure-response routine (N/S/E/W pulse via `MountPort.pulse_axis`, measure via `roi_tracker`, compute px/ms response matrix).
*Done when:* replaying `datasets/guiding/axis1_response`/`axis2_response` produces a calibration matrix within tolerance.

**Stage 5 — CollimationTool domain + application (heavy port)**
Port `domain/collimation_measurement.py`, `symmetry_analysis.py`, `diffraction_analysis.py`, `focus_metric.py`, `collimation_state.py` from `smart_telescope/domain/collimation/processing/*` + `models.py`. Port `application/collimation_controller.py` (from `services/collimation/assistant.py`), `focus_controller.py` (from `focus_search.py`/`fwhm_focus.py`), `recenter_policy.py` (from `PulseCenterer`, now consuming `roi_tracker.TrackingResult`, issuing corrections only via `MountPort.pulse_axis`). Characterization-test each ported algorithm before adapting, using donut/Bahtinov-spike generators added to `frame_factory.py`.
*Done when:* `pytest tests/core tests/collimation -v` green; a synthetic defocused-donut frame produces a collimation recommendation matching the ported logic within tolerance.

**Stage 6 — GuideTool domain + application (heavy port)**
Port `domain/guide_error.py`, `guiding_state.py` from `smart_telescope/domain/guiding.py`. Port `domain/correction_model.py` from `services/guide_measurement.py`, now consuming `roi_tracker` output instead of a stateless per-call target. Write `domain/drift_estimator.py` (check Stage-0 inventory notes first for a closer analog). Port `application/guide_controller.py` from `services/guiding_service.py`. Write `application/calibration_controller.py` (pairs with Stage 4's `axis_calibration.py`) and `correction_policy.py::GuideCorrectionPolicy` fresh — separate class from `CollimationRecenterPolicy`, both on the one shared `MountPort`.
*Done when:* `pytest tests/core tests/collimation tests/guide -v` all green (doc §6's rule: a guide-only-seeming change still runs all three); `datasets/guiding/lost_star` exercises `roi_tracker`'s LOST/SEARCHING/REACQUIRED path end-to-end from the guide side, proving the shared core is genuinely shared.

**Stage 7 — PySide6 UI (new territory, real minimal UI, not an empty shell)**
`collimation_tool/ui/`: live-view widget (frame → `QImage` via `frames.analysis_plane`), start/stop stream, exposure/gain controls bound to `CameraPort`, donut/spike overlay, recommendation readout. `guide_tool/ui/`: analogous live-view + start/stop guiding, drift-vector overlay, RMS/last-pulse readout. Both wire to `fake_camera`/`no_mount`/`replay_camera` by default in dev config (real adapters selected via config flag), so both are demoable on Windows without hardware.
*Done when:* `collimation-tool` and `guide-tool` console scripts both launch, show a live fake-camera-driven star image, and visibly do different things end-to-end.

**Stage 8 — Regression-protection hardening + packaging finish**
Fill out remaining contract-test factories; build the tolerance-based golden-master helper (`tests/integration/_golden_master.py`) and wire every `datasets/` leaf; write `CONTRIBUTING.md` (change-classification table, AI-patch-budget rule, "run core+collimation+guide regardless of classification" script); final `ruff check .` / `mypy --strict` / `lint-imports` / `pytest --cov-fail-under=80`; tag `v0.1.0`.

---

## Regression-protection scaffolding — concrete artifacts, not prose

- **Small public interfaces**: each `astrotool_core/<subsystem>/__init__.py` explicitly lists `__all__`; enforced by review convention (documented in `CONTRIBUTING.md`) since cross-module private-import prevention isn't fully automatable.
- **Dependency rule**: `[tool.importlinter]` contracts above, `lint-imports` run alongside `pytest` every time — real tooling, not just review.
- **Contract tests**: `tests/contracts/test_{camera,mount,focuser}_contract.py`, `@pytest.mark.parametrize("factory", [fake, replay, real])`, real-hardware factories `skipif`-guarded exactly like `smart_telescope/tests/fixtures/README.md`'s convention.
- **Characterization tests**: one `test_*_characterization.py` per ported-then-touched module, doc's exact template (pin observed behavior before refactoring). Rule: no PR touches a function in `packages/` or `apps/*/domain/` without one, unless the code is brand-new (TDD applies instead).
- **Golden-master tests**: `datasets/{collimation,guiding}/<scenario>/frames/*.fits` + `expected.json`, loaded via `testing/replay_dataset.py`, compared via a small tolerance helper:
  ```python
  def assert_matches_golden(actual: dict, expected_path: Path, *, tolerances: dict[str, float]) -> None:
      expected = json.loads(expected_path.read_text())
      for key, tol in tolerances.items():
          assert actual[key] == pytest.approx(expected[key], abs=tol), f"{key} drifted beyond tolerance {tol}"
  ```
  Tolerances defined and justified inline per test (target position ±0.5px, FWHM ±0.3px, drift vector ±0.2px/s, etc.).
- **Impact-scoped suites**: `tests/{core,collimation,guide,contracts,integration}` split as in the tree above; a `scripts/check.ps1`/`check.sh` always runs `pytest tests/core tests/collimation tests/guide` regardless of what changed, matching doc line "Auch dann, wenn angeblich nur das CollimationTool geändert wurde."
- **Change classification**: `CONTRIBUTING.md` documents the doc's `CORE-CAMERA / CORE-FRAME / ... / UI-GUIDE` labels and their expected test impact; process convention, tied to the always-run-all-three script above so under-scoped testing is structurally impossible even without a machine-checked label.
- **No incidental refactoring / AI-patch budget**: `CONTRIBUTING.md` reproduces the doc's own list (allowed files = explicitly named implementation files + their tests; no new fallback logic, no removed checks, no signature changes without migration) as the operative rule for this repo, plus a suggested `git diff --stat` file-count sanity check before proposing any patch as done.

---

## Verification

- After each stage: `pytest tests/<relevant-scope> -v` plus, from Stage 3 onward, `pytest tests/core tests/collimation tests/guide` in full (per the doc's own "always run all three" rule).
- `ruff check .`, `mypy --strict`, `lint-imports` clean at every stage boundary, not just at the end.
- Stage 3's replay-sequence test is the first real end-to-end proof the shared core works: `pytest tests/integration/test_roi_tracker_replay.py -v` should show the exact `[LOCKED, LOCKED, LOST, SEARCHING, REACQUIRED]` transition sequence from the doc.
- Stage 7's manual check: run `collimation-tool` and `guide-tool` (installed via `pip install -e ".[dev]"` console scripts) on Windows against the fake/replay adapters and confirm both launch, show a live image, and behave visibly differently.
- Final gate before tagging `v0.1.0`: full `pytest tests/ --cov-fail-under=80` green, `lint-imports` clean, both console scripts launch.
