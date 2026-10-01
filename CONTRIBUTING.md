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

## Test pyramid and the risk-based minimum gate

A large green suite is not proof that the change you made works. The first
proof of a change is the fast, focused test that exercises exactly that
behavior; broad suites are escalation and release gates, not the first proof.

The intended shape is:

```text
many pure unit tests
        ↓
many deterministic component / state-machine tests
        ↓
focused adapter contracts
        ↓
few integrations
        ↓
very few acceptance / end-to-end tests
        ↓
real hardware confirmation (final validation only)
```

| Tier | What it may touch | Target feedback |
|------|-------------------|-----------------|
| `unit` | pure / near-pure code; no Qt event loop, threads, wall-clock sleeps, hardware, INDI, network | seconds |
| `component` | one production component with fakes/simulators for its boundaries; fake clock where timing matters | seconds |
| `contract` | adapter/port conformance against deterministic fakes or the installed library's API shape | seconds |
| `integration` | several production components wired together; still no hardware | minutes |
| `acceptance` | small below-UI product workflow set (`datasets/acceptance/`) | minutes |
| `hardware` | real devices; explicit opt-in only | field run |

Minimum before a change is done:

- the unit/component tests directly covering the changed code — **mandatory**;
- the contract tests of every boundary the change touches — **mandatory**;
- a defect fix adds or strengthens a test at the **lowest practical tier**. A
  high-level acceptance test alone is not sufficient proof when the defect
  belongs to a lower-level component;
- integration / regressions / acceptance / full coverage — escalation when the
  change crosses components, and always before a release.

Every collected test carries exactly one tier marker, assigned from its path
by `tier_for()` in `tests/conftest.py` (an explicit `@pytest.mark.<tier>` on
the test wins; a test file no rule covers is a collection error):

| Path | Tier |
|------|------|
| `tests/core/**` | `unit`, except the modules listed in `CORE_COMPONENT_MODULES` (they drive a component through a fake boundary/clock, or use threads, sockets, subprocesses or sleeps) → `component` |
| `tests/{collimation,guide}/domain/` | `unit` |
| `tests/{collimation,guide}/{application,ui}/` | `component` |
| `tests/contracts/`, `tests/collimation/test_onstep_boundary.py` | `contract` |
| `tests/contracts/**::test_real_*` (real-device cases) | `hardware` |
| `tests/integration/`, `tests/regressions/`, `tests/local_data/` | `integration` |
| `tests/acceptance/` | `acceptance` |

`hardware` tests are skipped unless you opt in with `pytest --run-hardware`
(or `ASTROTOOL_RUN_HARDWARE=1`) — on top of their own device env vars
(`ASTROTOOL_ONSTEP_INDI`, ...). Coverage is **not** collected by a plain
`pytest` run; it runs in the release gate and CI's coverage job (threshold
80, `[tool.coverage.report]` in `pyproject.toml`).

Commands (`scripts/check.sh` flags in brackets; every mode runs ruff, mypy and
import-linter first):

```text
scripts/check.ps1                # [no flag]      tests for what you changed
scripts/check.ps1 -AllFast       # [--all-fast]   -m "unit or component or contract"
scripts/check.ps1 -Integration   # [--integration] -m "integration or acceptance"
scripts/check.ps1 -Release       # [--release]    full release gate + coverage
```

The default mode asks `scripts/changed_tests.py` which tests prove the files
changed vs the merge-base with `origin/main` (plus uncommitted and untracked
files). A production module `x.py` selects its own test modules
(`test_x.py` / `test_x_*.py`) in the matching directory —
`packages/astrotool_core/<sub>/` → `tests/core/<sub>/`,
`apps/<app>/<layer>/` → `tests/<app>/<layer>/` — and the whole directory
only when it has none; a changed port/adapter/null object (or anything in
`onstep`/`indi`) adds `tests/contracts`. A test file selects itself, a
nested `conftest.py` or test helper its directory, `datasets/<kind>/` the
tests reading that kind. Anything it can't map safely (`tests/conftest.py`,
`pyproject.toml`, `requirements*.txt`, shared test doubles — anything in
`astrotool_core.testing` or any `fake_*.py` — unknown paths) falls back to
all fast tiers. Pass at most one mode switch; combining them is an error.

Measured wall-clock per tier: [`docs/quality/test-tier-timings.md`](docs/quality/test-tier-timings.md).

Production deployment (the Raspberry Pi) and real hardware are the **final
validation step**, never the normal environment for reproducing a bug.

## Deterministic time

Application and UI policy code must not call `time.sleep()` or poll the wall
clock directly for policy waits (settle times, retries, confirmation timeouts,
deadlines). Express them through the injected clock/scheduler boundary (issue
#53) so tests can advance fake time instantly and hit exact deadline
boundaries. Hardware adapters may block internally where the external API
requires it; each such exception is listed with its reason in the sleep-guard
allowlist. Tests must not use multi-second real-time waits to observe state.

Use `astrotool_core.timing`: take `clock: Clock | None = None` (fall back to
`SYSTEM_CLOCK`) and wait with `clock.sleep(seconds, cancel)`,
`Deadline.after(timeout, clock=...)` or `poll_until(...)`. A deadline is
expired when `now >= expires_at`. In tests pass `FakeClock()`: `sleep`
advances fake time instantly and is recorded in `clock.sleeps`; use
`call_at`/`call_later` to make a device finish or a Stop arrive *during* a
wait, and `FakeClock(auto_advance=False)` with `wait_for_sleepers()`/`advance()`
only when a test must observe a worker mid-wait (read the "Limits" section of
`fake_clock.py` first). Cover each timeout just before / exactly at / just
after the boundary, and make sure it is the *production* check that decides
the boundary, not the test double. `tests/core/testing/test_no_policy_sleep.py`
fails on any direct `time.sleep` in production code outside its allowlist and
on an allowlist entry that no longer sleeps; an exception needs a stated
external-API reason there. (Known gap: it does not yet catch `Event.wait` used
as a sleep or wall-clock polling loops — tracked in the restructuring tracker.)

## Duplicated knowledge

Every device name, slot count, limit, default, timeout representing one
external behavior, capability rule, and mode/tracking decision has **exactly
one authoritative owner** (configuration object, domain policy, application
service, or adapter). Do not add a second fallback literal "just in case" — a
contradictory fallback is how the wrong EFW device name shipped. Similar-looking
code is not automatically a defect; duplicated *authoritative knowledge* is
(issue #55).

## Proof manifests

Every bug fix and every HIGH/CRITICAL issue ships a small machine-readable
proof manifest (issue #54) naming the issue, the affected modules, the
regression tests, whether each test failed before the fix, any dataset UUID,
the single focused command that runs the proof, the broader tier required, and
any remaining field-only assumption. Review asks one question first: **what
test would fail if this bug returned?** The manifest must answer it.
Documentation-only changes need none.

The manifest is `proofs/<issue>.toml` (`<issue>-<slug>.toml` for a second fix
under one issue, `diag-<uuid8>-<slug>.toml` for a field fix without an issue);
the schema is in `proofs/README.md`. In short:

```toml
issues = [49]
summary = "what broke, why, what the fix does"
modules = ["apps/collimation_tool/ui/mount_test_move_panel.py"]
fix_commits = ["fed00fa"]
dataset_uuids = ["6d33f37c"]            # diagnostic bundle(s), [] when none
focused_command = "pytest -q tests/collimation/ui/test_calibration_failure_recovery.py::TestBundle6d33f37cRegression"
broader_tier = "component"
field_only_assumptions = []
gaps = []                               # optional: missing lower-tier tests, said honestly

[[regression_tests]]
node = "tests/collimation/ui/test_calibration_failure_recovery.py::TestBundle6d33f37cRegression::test_a_camera_that_never_delivers_a_fresh_frame_can_be_retried_over_and_over"
failed_before_fix = true
evidence = "revert-proof: ...test_calibration_failure_recovery.py:481: AssertionError: attempt 0: event loop blocked"
# or: failed_before_fix = false + reason_no_prefix_repro = "why it can't be shown"
```

The proof for one issue is a single command:

```text
python scripts/prove.py <issue>         # e.g. 49: every manifest listing that issue
python scripts/prove.py diag-7b21bdf1   # a manifest by unique stem (or UUID) prefix
python scripts/prove.py --validate-all  # schema, flags, files, node ids, tiers, fix commits
```

All selected manifests run; a PASS/FAIL line per manifest is printed. The focused
command may only use flags that cannot drop a listed test (`-q`, `-x`, `--tb`,
`-r`, ... -- never `-k`, `-m`, `--deselect`, `--co`; the list is in
`proofs/README.md`).

`failed_before_fix = true` needs evidence naming the failing line
(`path.py:NNN`): revert just the fix in a **separate git worktree** created
from the current HEAD (never in the shared tree), run `scripts/prove.py <issue>`
there (it puts that worktree's `packages/`/`apps/` first on `PYTHONPATH`), and
copy the failing assertion line into `evidence`. A defect with no low-tier test
is recorded under `gaps`, not hidden.

CI does **not** run every focused command: the listed regression tests run in
their normal tiers in the fast and slow jobs. Validation therefore rejects a
listed test that would not run there -- tier `hardware` (per `tier_for()` in
`tests/conftest.py`), `tests/local_data/`, or a static `skip`/`skipif`/`xfail`
marker. CI's fast job (full git history) runs `scripts/prove.py --validate-all`
and `tests/contracts/test_proof_manifests.py`; the PR template asks the same
question and points to the manifest.

## Agents and issue ownership

This repository uses common code ownership. Work is split by **issue**, not by
layer — there is no permanent "camera agent", "UI agent" or "mount agent":
failures here live *between* camera, mount, timing, orchestration and UI.

- One implementing agent owns one issue: the implementation and its low-level
  regression tests. Two agents never share responsibility for one change.
- A separate review agent then acts as an independent test adversary — it tries
  to find cases the implementer missed rather than extending the solution.
- Before an agent starts, it gets a written task contract:
  allowed production files/modules · behavior being changed · tests to
  add/change · interfaces that must not change · dependencies on other
  issues · explicit non-goals · proof required for completion.
- Anything discovered outside that contract becomes a recorded dependency
  (another issue or a tracker entry), not an opportunistic edit.
- Shared architectural foundations (test taxonomy #50, simulators #51,
  application services #52, time #53) are serialized where their code
  overlaps: the time abstraction is established once and consumed by the
  simulators, never invented twice. Parallel work is safe only between
  contracts with disjoint production files.

## Proof of solution is the team's responsibility

Do not treat field testing by the user as the primary way to prove that a fix works.

For a reported defect, especially one that is intermittent, timing-sensitive, hardware-dependent, or hard to reproduce manually, the implementation work must include enough automated or deterministic verification to demonstrate that the specific failure mode is fixed before asking the user to try it again.

The expected workflow is:

```
reproduce or characterize the failure
-> write a failing regression test or deterministic simulation
-> implement the fix
-> prove the regression now passes
-> run the relevant broader test gates
-> only then ask for field confirmation where real hardware is still required
```

Field confirmation is valuable, but it is the final validation layer, not a substitute for engineering proof.

For hardware-facing code, use fake-INDI/integration fixtures, deterministic timing/state simulations, captured diagnostics, and real regression datasets where possible. Tests should verify observable requirements such as command sequence, bounded timing, state transitions, failure recovery, and UI responsiveness — not merely that a function was called.

When a bug cannot be fully reproduced in CI because the real hardware behavior is unavailable, the team must still:

- identify the smallest behavior that can be reproduced deterministically;
- encode that behavior as a permanent regression test;
- add instrumentation that proves the remaining hardware-specific assumptions in one field run;
- document exactly what is still unproven and why;
- avoid pushing repeated speculative fixes that require the user to act as the test harness.

A fix is not considered complete merely because the code looks plausible or the generic suite is green. The issue's specific acceptance criteria must have explicit verification evidence.

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

This runs ruff/mypy/lint-imports plus every test directory
(`tests/core tests/collimation tests/guide tests/contracts tests/integration
tests/regressions tests/acceptance`) with coverage (`fail_under = 80`).

## Server-side quality gate

`.github/workflows/quality.yml` runs on every push/PR to `main` as three
**parallel** jobs, each named after its tier so a failure says which tier
broke:

- **`fast tier: lint + unit/component/contract`** — `ruff check .`, `mypy .`,
  `lint-imports`, then `pytest -m "unit or component or contract"`, no
  coverage.
- **`slow tier: integration/regressions/acceptance`** —
  `pytest -m "integration or acceptance"` (~2 min, so an integration
  failure is reported long before the full suite finishes).
- **`release gate: full suite + coverage`** — the release gate: the same
  test directories as `scripts/check.sh --release` with `--cov`
  (fail-under 80), uploads `coverage.xml`, then runs
  `scripts/quality_report.py` and uploads the hotspot report.

The jobs deliberately don't wait for each other: the coverage job runs
every test anyway, so chaining it after `fast` would roughly double the
wall-clock time. In parallel, the whole gate takes about as long as the
release gate alone, lint lands within minutes, and the overlap costs runner
minutes rather than waiting time.

All jobs use Python 3.13.13 and call the same tools with the same config, not
a separate copy of the thresholds, so local and CI runs cannot drift apart.
This is the authoritative merge/release feedback mechanism: a failing check
blocks the PR regardless of what a local run showed. Running
`scripts/check.sh --release` locally before pushing a release is still
recommended so failures are caught before CI, not instead of it.

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
and in the CI fast-tier job, which is the enforcement point for every change
and release.

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
a bounded recent-log tail, any recent frames as raw FITS (`frames/`), and
any provided images as PNG (`images/`) — whenever:

- an unhandled exception reaches the app's `sys.excepthook` boundary
  (installed in each app's `main.py`), or
- the user clicks **Capture diagnostics** in either app's toolbar.

Both paths go through the same `DiagnosticService._capture()`, so bundle
format never diverges between automatic and manual capture. Each
`MainWindow` registers itself as the service's context/frame/image
provider (`set_context_provider`/`set_frame_provider`/
`set_image_provider`) so an automatic capture — which has no call-site
context of its own — still gets the latest known measurement/state, a
small bounded recent-frame buffer (kept in the UI layer, not the
service: see each `MainWindow`'s docstring), and (CollimationTool only)
each panel's actually-*displayed* pixmap as PNG. That last one matters
specifically for FOV-overlay reports: `frames/*.fits` is raw,
unstretched sensor data with no demosaicing or overlay drawn — it can't
show whether a "Calibrate FOV" polygon looks visually wrong, only
`images/*_display.png` can. `incident.json`'s context also includes
`fov_calibration` (the `CrossCameraRegistrationResult` behind whatever
polygon is currently shown, if any) once CollimationTool has completed at
least one calibration.

Retention is local and bounded — bundles older than 7 days, or beyond the
most recent 20, are pruned on every capture (`DEFAULT_MAX_AGE_DAYS`/
`DEFAULT_MAX_BUNDLES`). Nothing is uploaded automatically, and dict keys
that look sensitive (`password`, `secret`, `token`, ...) are redacted
before a bundle is written.

To investigate a reported incident locally: `astrotool_core.diagnostics.
find_bundle("<uuid-or-prefix>")` resolves it to its directory (a full UUID
or an unambiguous prefix both work) — no service/database lookup needed,
just the directory convention above.

## Real-world regression datasets

A reproducible field defect in image analysis / calibration / registration
should become a **permanent regression case** when a diagnostic bundle captured
the actual frames that exposed it: `reproduce → failing test → fix → permanent
passing test` (issue #6).

Capture it under `datasets/regressions/<issue-id>/` — the schema and the
`scripts/regression_dataset.py` intake helper are documented in
[`datasets/regressions/README.md`](datasets/regressions/README.md). Two rules
that come up in review:

- **Expected values are independently authored.** Each case's `rationale` in
  `expected.json` must state how its expected result was derived from something
  *other than* the implementation under test — a hand measurement, a known
  software transform, a physical ground truth, or "must reject, by inspection".
  Never paste `measure_translation_offset()` / the calibrator's own output as
  the expectation.
- **Real data is additive, not a replacement.** The synthetic
  `datasets/acceptance/` scenarios stay the deterministic baseline.

`tests/regressions/` runs in `scripts/check.sh --release` and CI. A
`datasets/regressions/<id>/` directory with a malformed `expected.json`, or with
no wired test, fails the build rather than becoming dead data.

### Getting the frames off the rig

A field failure is captured as a bundle on the Pi itself
(`~/.CollimationGuideTool/diagnostics/<uuid>/`). Pull it to the dev box before
scaffolding:

```bash
python scripts/pull_diagnostic_bundle.py --uuid <uuid>     # ssh+scp from rasppi3
python scripts/regression_dataset.py --uuid <uuid> --issue <n>
```

`pull_diagnostic_bundle.py` is the only part of this tooling that touches the
network; `regression_dataset.py` never does. `--host` overrides the ssh target
(default `rasppi3`, from `~/.ssh/config`).

### Comparing translation-estimator algorithm changes

`measure_translation_offset()`'s real-frame corpus (issue #28) is a separate
workflow from dataset intake above — it's for evaluating/optimizing the
estimator itself, not pinning one field bug. Run
`python scripts/translation_estimator_benchmark.py` before and after a
change to compare (writes `docs/quality/translation_estimator_benchmark.
{json,md}`; measurement-only, no threshold enforced, same philosophy as
`scripts/quality_report.py`). See `datasets/regressions/28/README.md` for
the corpus layout and `astrotool_core.testing.shift_grid` for the shared
known-shift grid.

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
