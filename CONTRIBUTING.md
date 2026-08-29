# Contributing

This project's central risk is not missing features — it's uncontrolled
changes crossing module boundaries and silently affecting the other app.
These rules exist to make that structurally hard, per
[`collimation-guidetool-architektur.md`](collimation-guidetool-architektur.md).

## Before touching existing, working code

**Characterize it first.** If you are refactoring or adapting an
already-working function (most obviously true for anything ported from
`smart_telescope`), write a test that pins its *current, observed* behavior
before you change anything:

```python
def test_existing_roi_reacquisition_behavior():
    frames = load_replay("collimation_star_moves_after_adjustment")
    results = run_roi_tracker(frames)
    assert results.lock_states == [LOCKED, LOCKED, LOST, SEARCHING, REACQUIRED]
    assert results.final_target == pytest.approx(Point(812.4, 463.1), abs=0.5)
```

This does not apply to brand-new code with no prior behavior (e.g.
`roi_tracker.py`'s lock-state machine, `pixel_format.py`) — write those
test-first (TDD) instead.

## Before any change

Classify it as one of:

```
CORE-CAMERA   CORE-FRAME   CORE-TARGET   CORE-MOUNT   CORE-FOCUS   CORE-SESSION
APP-COLLIMATION   APP-GUIDE
UI-COLLIMATION   UI-GUIDE
```

Regardless of the label, run at minimum:

```
pytest tests/core tests/collimation tests/guide
```

before considering any change done — **even a change that only touches
CollimationTool**. `scripts/check.sh` / `scripts/check.ps1` runs this plus
lint/type-check in one step.

A `CORE-*` change additionally requires the full `tests/contracts` and
`tests/integration` suites to pass.

## Before pushing a release

`tests/acceptance` is a synchronous, below-UI regression suite (deterministic
synthetic donut/star scenarios driven straight through `CollimationController`
/ `GuideController.process_frame()`, with expected values reviewable in
`datasets/acceptance/*.json`). It's slower and broader than the per-change
suites above, so it isn't run on every patch — but it must pass before pushing
a release/tag to GitHub:

```
scripts/check.sh --release      # or: scripts/check.ps1 -Release
```

This runs `tests/core tests/collimation tests/guide tests/contracts
tests/integration tests/acceptance` plus ruff/mypy/lint-imports, on top of
the always-run-three suites above.

## Server-side quality gate

`.github/workflows/quality.yml` runs on every push/PR to `main` and executes
exactly the release gate above (Python 3.13, `ruff check .`, `mypy .`,
`lint-imports`, then the same six test directories) — it calls the same
tools with the same config, not a separate copy of the thresholds, so local
and CI runs cannot drift apart. This is the authoritative merge/release
feedback mechanism: a failing check blocks the PR regardless of what a local
run showed. Running `scripts/check.sh --release` locally before pushing is
still recommended so failures are caught before CI, not instead of it.

Known environment gap: `ubuntu-latest` doesn't ship the Qt runtime
libraries PySide6 dynamically links even under `QT_QPA_PLATFORM=offscreen`
(`libEGL.so.1` and several `libxcb-*`/`libxkbcommon*` libraries) — every UI
test module fails to *import* (not just run) without them. The workflow
installs them via `apt-get` before the dependency-install step. This bit
us once (the workflow ran green-looking locally on Windows, which needs
none of this, while every CI run silently failed from the day the gate was
added) — if a future UI import starts pulling in a new native Qt module,
check this list first before assuming the test itself is broken.

## What one patch may touch

```
Allowed:
- the explicitly named implementation file(s) for this change
- their corresponding test file(s)

Not allowed:
- files outside that list
- new fallback logic that wasn't requested
- removing an existing check/validation
- replacing a working adapter
- changing a public signature without a migration note
```

A patch that "while I was in there" touches many additional files is not
acceptable even if the tests happen to stay green. If a refactor is
genuinely needed, it is a separate, behavior-neutral commit — green tests
before and after, no functional change in the same commit.

## Cyclomatic complexity

Ruff's McCabe checks (`C90`) are enabled with a hard limit of
`max-complexity = 15` (`[tool.ruff.lint.mccabe]` in `pyproject.toml`), run as
part of the normal `ruff check .` in `scripts/check.sh` / `scripts/check.ps1`
— no separate CI system exists in this repo, so that script *is* the
enforcement point for every change and release.

Interpretation:

- CC 1–5: simple / very good
- CC 6–10: normal
- CC 11–15: review zone — acceptable when the branching is cohesive domain
  logic and well tested; don't fragment it artificially just to lower the
  number
- CC >15: refactor, or justify explicitly in the PR/commit message before
  merging
- CC >20: should normally not be accepted

Prefer preserving a cohesive domain algorithm (donut analysis, autofocus
search, tracking state handling) over splitting it into pieces that only
exist to dodge the metric. Refactor when complexity instead reflects
multiple responsibilities, duplicated decisions, or branches that are hard
to test in isolation. No broad `# noqa: C901` suppression — a genuine
exception is scoped to the one function, with a comment saying why.

As of the `C90` rollout, the highest-complexity functions in the codebase
were reviewed and found to be legitimate cohesive domain logic, well inside
the limit: `FocusSearcher.search()` (12), `CollimationRecenterPolicy.center()`
(10), `DonutAnalyzer.analyze()` (8), `CollimationAdvisor.recommend()` (7),
`GuideController._loop()` (7), `RoiTracker.update()` (6). None required
refactoring.

## Complexity/coverage hotspot baseline (CRAP)

`scripts/quality_report.py` combines per-function complexity (radon) with
per-function statement coverage (from the `.coverage` data `pytest` already
writes) into a CRAP score — `complexity^2 * (1 - coverage)^3 + complexity` —
and writes `docs/quality/hotspots.json` (every function) and
`docs/quality/hotspots.md` (top 10, committed as the current baseline).
Regenerate after a normal `pytest` run:

```
pytest
python scripts/quality_report.py
```

This is measurement-first and informational: it does not enforce a
threshold (see issue #9, blocked on this baseline plus a mutation-testing
baseline from issue #5) and must never be used to justify refactoring on its
own — issue #7 requires a hotspot to show a *material* risk (high complexity
*and* weak coverage) before touching it, not complexity alone. Note radon's
complexity numbers are not ruff's (see the disclaimer in `hotspots.md`) —
ruff's `C90` gate above remains the enforced threshold.

## Mutation-testing baseline

`mutation/` holds one [cosmic-ray](https://cosmic-ray.readthedocs.io/) config
per module for a selective mutation-testing baseline (issue #5), scoped to
pure/near-pure deterministic domain modules where results are fast and
meaningful: `correction_model.py`, `collimation_measurement.py`,
`collimation_state.py`, `roi_tracker.py`. UI, threading code, and the
hardware adapters are deliberately excluded from this first iteration — see
`mutation/README.md` for rationale and how to rerun.

`docs/quality/mutation.md` (committed) is the current baseline —
`scripts/mutation_report.py` regenerates it from the four sessions. Like the
CRAP baseline, this is measurement-first and informational: no score
threshold is enforced yet (issue #9), and a surviving mutant is not itself a
bug — issue #8 reviews survivors individually before any test is added, to
tell a real missing behavior apart from an equivalent or irrelevant mutation
(e.g. mutating a docstring-adjacent literal, or a `<` vs `<=` where the
boundary is genuinely never hit).

## Diagnostic capture

`astrotool_core.diagnostics.DiagnosticService` (issue #10) writes a
self-contained, UUID-named bundle to
`~/.CollimationGuideTool/diagnostics/<uuid>/` — structured `incident.json`,
a bounded recent-log tail, and any recent frames as FITS — whenever:

- an unhandled exception reaches the app's `sys.excepthook` boundary
  (installed in each app's `main.py`), or
- the user clicks **Capture diagnostics** in either app's toolbar.

Both paths go through the same `DiagnosticService._capture()`, so bundle
format never diverges between automatic and manual capture. Each
`MainWindow` registers itself as the service's context/frame provider
(`set_context_provider`/`set_frame_provider`) so an automatic capture —
which has no call-site context of its own — still gets the latest known
measurement/state and a small bounded recent-frame buffer (kept in the UI
layer, not the service: see each `MainWindow`'s docstring).

Retention is local and bounded — bundles older than 7 days, or beyond the
most recent 20, are pruned on every capture (`DEFAULT_MAX_AGE_DAYS`/
`DEFAULT_MAX_BUNDLES`). Nothing is uploaded automatically, and dict keys
that look sensitive (`password`, `secret`, `token`, ...) are redacted
before a bundle is written.

To investigate a reported incident locally: `astrotool_core.diagnostics.
find_bundle("<uuid-or-prefix>")` resolves it to its directory (a full UUID
or an unambiguous prefix both work) — no service/database lookup needed,
just the directory convention above.

## Public interfaces

Each `astrotool_core/<subsystem>/__init__.py` is the only supported import
surface for that subsystem:

```python
# OK
from astrotool_core.camera import CameraPort, Frame

# Not OK — reaches into a private implementation module
from astrotool_core.camera.touptek_adapter import _SdkCallbackHandler
```

## Dependency direction

Enforced by `import-linter` (`lint-imports`), not just review:

- `astrotool_core` never imports `collimation_tool` or `guide_tool`.
- Adapters (`camera/touptek_adapter.py`, `mount/indi_adapter.py`) never
  import a UI toolkit.
- `target/roi_tracker.py` never imports `astrotool_core.mount` — it reports
  a measured deviation; it never moves anything.

## External adapters

`mount/indi_adapter.py` wraps the `onstep-adapter` pip package. Never edit
that package's internals from this repo — if it's missing something this
project needs, flag it and wait rather than patching around it locally.
