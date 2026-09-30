# Restructuring tasks — audit follow-up (#50–#55, #48, open HIGH issues)

Single tracker for the audit-driven restructuring. Updated in the same commit as
each increment.

## Sprint goal

Raise change safety: move device/workflow policy out of Qt panels into small
application services, make time deterministic, give every hardware boundary an
executable simulator, and make the fast, focused test the default proof —
with real hardware as the final confirmation only.

**Rollout rule:** no Raspberry Pi rollout (`git reset --hard origin/main` on the
Pi) until every increment below is `done` or `field-only`. Increments are
pushed to `main` one at a time with CI green; the Pi is not synced in between.

## Execution model

- Common code ownership; an agent owns an **issue**, never a layer.
- One implementing agent per issue: implementation + its low-level regression tests.
- One independent review agent per implementation, acting as a test adversary.
- Coordinator (main session) integrates, runs the gate, pushes, updates this file.
- Out-of-scope discoveries become a dependency entry below, not an unplanned edit.
- Shared foundations (#50, #51, #52, #53) are serialized where their code overlaps.

## Dependency graph

```text
#50 test taxonomy / commands
   ├── #53 clock + scheduler
   │      └── #51 hardware simulators
   └── #54 proof manifests
#52 application-service extraction
   └── #48 and the existing HIGH workflow issues
#55 duplication audit — analysis first; each removal is its own small change
```

## Task table

Status: `todo` · `in-progress` · `done` · `field-only` (only real hardware can close it)

| ID | Issue | Task | Owner | Review | Status | Commit | Proof command |
|----|-------|------|-------|--------|--------|--------|---------------|
| S0 | #50–#55 | Tracker + CONTRIBUTING.md / AGENTS.md aligned with the audit | coordinator | — | done | | docs only |
| S1 | #50 | Test tier markers, fast/changed gate, coverage split, CI jobs, timing report | issue agent | APPROVE-WITH-FIXES → 4 fixes applied, re-verified | done | (this commit) | `scripts/check.ps1` (default mode) · `pytest tests/core/testing` |
| S2 | #54 | Proof manifests, `scripts/prove.py`, CI validation, 5 backfills (3 revert-proven) | | | todo | | |
| S3 | #53, #13 | `astrotool_core.timing` (Clock/FakeClock/Deadline/Scheduler); migrate timing modules; sleep guard | | | todo | | |
| S4 | #51 | Scenario simulators (OnStep mount, focuser, filter wheel, ToupTek camera) on FakeClock; 5 defect reproductions | | | todo | | |
| S5 | #55, #52 | Duplication inventory + size/dependency baseline (`docs/quality/duplication-audit.md`) | analysis agent | coordinator spot-check (P01 confirmed in code) | done | (this commit) | docs only |
| S6.0 | #46, #31, #55 (P01/P02) | **CRITICAL, promoted:** Mount Align bootstrap/nudge/screen moves go through `pulse_axis`, which production `OnStepMountPulseAdapter` always refuses; no rate is ever installed, so angular moves never start on the rig. Size the first move angularly from the optics seeds instead of a timed bootstrap; failing regression against the real adapter + fake OnStep connection first | | | todo | | |
| S6.1 | #55 | Single-source device defaults + config-source contract test | | | todo | | |
| S6.2 | #52 | `DeviceConnectionService` (Focuser → MountPark → MountTestMove) | | | todo | | |
| S6.3 | #52 | `OperationLifecycle` + bounded Busy timeout (FilterWheel, Focuser) | | | todo | | |
| S6.4 | #48 | Remove Mount Align **and autofocus** local Star/Terrestrial toggles; derive from global OperatingMode (M04 decided) | | | todo | | |
| S6.5 | #55 | ToupTek capability table (unsupported features contract-driven) | | | todo | | |
| S6.6 | #52 | Mount Align orchestration out of `mount_test_move_panel.py` into application layer | | | todo | | |
| S6.7 | #52 | Fresh-frame-after-motion service shared by Mount Align + autofocus | | | todo | | |
| S6.8 | #55, #31, #46 | Verify no competing movement path remains | | | todo | | |
| S7 | #15 #21 #29–#33 #35 #37 #39 #43 #44 #46 #49 | Proof manifests + missing low-tier tests; field-only assumptions listed | | | todo | | |
| S8 | all | Release gate green, issue evidence comments, ask user before Pi sync | coordinator | — | todo | | |

## Task contracts

Each issue agent receives its contract before it starts. Fields: allowed
production files · behavior changed · tests to add/change · interfaces that must
not change · dependencies · non-goals · proof required.

*(Contracts are added here as each increment starts.)*

### S1 — #50 test tiers and fast gate

- **Allowed files:** `pyproject.toml` (`[tool.pytest.ini_options]`, markers, coverage config only);
  `tests/conftest.py` (marker auto-assignment hook only); `scripts/check.ps1`, `scripts/check.sh`;
  new `scripts/changed_tests.py`; `.github/workflows/quality.yml`; new
  `docs/quality/test-tier-timings.md`; CONTRIBUTING.md sections "Test pyramid…" and
  "Server-side quality gate"; new tests under `tests/core/testing/` or `tests/contracts/`
  for the tier-assignment and changed-test mapping logic.
- **Behavior changed:** developer/CI test selection only. No production code changes.
- **Tests to add:** tier auto-assignment (every collected test gets exactly one tier marker);
  `changed_tests.py` path mapping (pure unit tests); "hardware" tests skipped unless opted in.
- **Frozen interfaces:** every existing test must still be collected and pass in the release
  gate; `scripts/check.ps1 -Release` / `check.sh --release` keep their meaning; coverage
  threshold (80) still enforced in the release/coverage job.
- **Dependencies:** none (foundation). #53/#54 build on its markers.
- **Non-goals:** moving/renaming test files; rewriting slow tests; changing production code;
  lowering coverage; hiding flaky tests.
- **Proof:** timing report with before/after wall-clock per tier; a pure-domain change proven
  by a targeted command that does not run UI/guide suites; CI shows separate fast/slow/coverage
  jobs; full release gate green.

### S5 — #55 + #52 duplication inventory (analysis only)

- **Allowed files:** new `docs/quality/duplication-audit.md` only.
- **Behavior changed:** none. No production or test code edits.
- **Content:** every duplicate candidate across `camera_panel.py`, `focuser_panel.py`,
  `filter_wheel_panel.py`, `mount_park_panel.py`, `mount_test_move_panel.py`, runners/calibrators,
  config/registry code — in #55's eight categories — classified as *intentional difference* /
  *must centralize (owner named)* / *superficial*, with file:line evidence and existing test
  coverage; baseline file sizes, largest functions, and a dependency-direction snapshot.
- **Dependencies:** none; feeds S6.x contracts.
- **Non-goals:** fixing anything; proposing a mega-controller.
- **Proof:** every "must centralize" item names its owner and the S6 step that removes it.

## Done log

| Date | Task | Commit | What was proven |
|------|------|--------|-----------------|
| 2026-10-01 | S1 | (this commit) | 1871 tests partition exactly into 6 tiers; single-file changes proven in 1.5–3.1 s (was a 14.5 min gate); CI fast/slow/coverage run in parallel; coverage ≥80 still enforced. **Not yet faster:** full component tier 801 s — 15 Mount Align panel tests alone take 459 s of real settle/wait time → payoff comes with #53/#51 |
| 2026-09-30 | S0 | (this commit) | docs only — guidelines now state the tiered gate, deterministic-time, duplicated-knowledge, proof-manifest, layering and agent-ownership rules |

## Outstanding / field-only assumptions

*(Filled per increment — anything that only real hardware can confirm.)*

- #50 closure still needs: deterministic low-tier tests for the mapped defect classes (connect
  exception, stuck Busy, shared OnStep concurrency, capability polling) — delivered by S3/S4;
  component-tier wall time reduced once the 15 slow Mount Align tests use fake time (S3/S6.6).

## Discovered dependencies

*(Out-of-scope findings recorded instead of acted on.)*

- **2026-09-30, S5 finding P01/P02 → S6.0.** `OnStepMountPulseAdapter.pulse_axis` refuses unconditionally
  (`_NO_PULSE_PRIMITIVE`); `MountTestMoveRunner._move_with_retry` only goes angular when a rate is
  installed, and `_learn_rate` installs one only after a successful *timed* move. Also
  `recenter_policy.py` reacquisition uses `pulse_axis`. No OnStepAdapter request exists for a timed
  pulse over INDI — none is needed: `move_angular` uses the degree-target move (OnStepAdapter #14)
  and ignores rates. Fix is in this repo. Explains why #46/#31 cannot pass a field run as-is.
- **S5 open question M04 — decided 2026-09-30 by the user:** the autofocus panel's local
  Star/Terrestrial selector is removed; the metric follows the global OperatingMode
  (Terrestrial → Tenengrad, Astronomical → star/FWHM). Folded into S6.4.
- **S5 live contradictions (9)** are listed in `docs/quality/duplication-audit.md`; each is owned by an
  S6 step and gets a failing regression before its fix.
