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
| S2 | #54 | Proof manifests, `scripts/prove.py`, CI validation, 6 backfills (5 revert-proven) | issue agent | APPROVE-WITH-FIXES → 10 fixes (flag whitelist, tier/skip check, evidence format, fetch-depth 0) | done | (this commit) | `python scripts/prove.py <issue>` · `--validate-all` |
| S3a | #53, #13 | `astrotool_core.timing` (Clock/FakeClock/Deadline/poll_until); acquisition, autofocus, tracking_mode, guide controller migrated; sleep guard | issue agent | APPROVE-WITH-FIXES → 9 fixes, mutation-proven | done | (this commit) | `pytest tests/core/timing tests/core/acquisition tests/core/mount tests/core/testing tests/guide tests/collimation/application` |
| S3b | #53 | Migrate `mount_test_move_runner.py`, `recenter_policy.py` (after S6.0); panel wall-clock polling; interruptible waits | | | todo | | |
| S4 | #51 | Scenario simulators (OnStep mount, focuser, filter wheel, ToupTek camera) on FakeClock; 5 defect reproductions | | | todo | | |
| S5 | #55, #52 | Duplication inventory + size/dependency baseline (`docs/quality/duplication-audit.md`) | analysis agent | coordinator spot-check (P01 confirmed in code) | done | (this commit) | docs only |
| S6.0 | #46, #31, #55 (P01/P02) | **CRITICAL, promoted:** Mount Align bootstrap/nudge/screen moves go through `pulse_axis`, which production `OnStepMountPulseAdapter` always refuses; no rate is ever installed, so angular moves never start on the rig. Size the first move angularly from the optics seeds instead of a timed bootstrap; failing regression against the real adapter + fake OnStep connection first | issue agent | 2 rounds: APPROVE-WITH-FIXES → APPROVE | done | 787ceed | `python scripts/prove.py 46-mount-align-angular-path` |
| S6.0b | #39, P01 | Guide-assisted reacquisition (`recenter_policy.py:113`) calls `pulse_axis` on the production OnStep adapter → always `pulse_rejected`, adapter message dropped; move to the angular path (after S6.0). **Must use the same ms↔arcsec factor S6.0 stores the matrix in (equivalent ms at `calibration_center_rate_x`·sidereal) — that factor needs one public owner first** | | | todo | | |
| S6.0c | #49, AGENTS manual-move rule | `OnStepMountPulseAdapter.move_angular` holds `connection.operation_lock` for the whole blocking GOTO (up to 30 s); `abort()` takes the same lock on the GUI thread before `stop()`, and park-panel status polls also take it → Stop would freeze the UI and not cancel; `_cancel` never checked. Characterize with a failing component test, then decide stop/lock semantics (verify OnStepAdapter's real abort semantics — no assumptions) | | | todo | | |
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

### S2 — #54 proof manifests

- **Allowed files:** new `proofs/` (manifests + README with schema); new `scripts/prove.py`; (extended after review: the `proofs/**`→manifest-tests mapping rule in `scripts/changed_tests.py` + its unit test); new
  `tests/contracts/test_proof_manifests.py` (+ unit tests for prove.py under `tests/core/testing/`);
  new `.github/pull_request_template.md`; `.github/workflows/quality.yml` (fast job: manifest
  validation step only); CONTRIBUTING.md section "Proof manifests" only; conftest tier table only
  if a new test module needs a tier entry.
- **Behavior changed:** none in production. Revert experiments run in a **separate git worktree**,
  never in the shared tree.
- **Tests:** manifest schema/existence validation; prove.py selection logic.
- **Frozen:** all production code; existing tests.
- **Dependencies:** #50 (tiers) done.
- **Non-goals:** writing new regression tests for old defects (only reference existing ones; missing
  ones → recorded as S7 gap); manifests for S6/S7 issues (they ship their own).
- **Proof:** ≥5 backfilled manifests; ≥3 revert-proven (focused test fails with fix reverted); #43 and
  #49 manifests present; `python scripts/prove.py 49` runs the proof in one command.

### S3a — #53 time abstraction (first half)

- **Allowed files:** new `packages/astrotool_core/timing/` (public via `__init__` only);
  `packages/astrotool_core/acquisition/stable_frame_acquisition.py`, `motion_aware_acquisition.py`;
  `apps/collimation_tool/application/autofocus_controller.py`, `autofocus_search.py`;
  `packages/astrotool_core/mount/tracking_mode.py`; guide-controller timing (`apps/guide_tool/application/`)
  for #13; their tests; new sleep-guard test; `tests/conftest.py` tier table entries only;
  `pyproject.toml` import-linter contract for `timing` only.
- **Excluded until S6.0 merges:** `mount_test_move_runner.py`, `mount_test_move_panel.py`,
  `recenter_policy.py` (→ S3b).
- **Behavior changed:** none (behavior-neutral injection; production defaults to the real clock).
- **Tests:** fake-time tests incl. deadline boundaries (before/at/after) and cancellation per wait
  state; #13 tests without the 2 s real wait.
- **Frozen:** public constructor signatures stay backward compatible (clock/scheduler are optional
  keyword args); no hardware op moved onto the Qt main thread.
- **Non-goals:** simulators (#51); changing algorithms or timeouts.
- **Proof:** ≥20 fake-time tests; wall-time before/after of the migrated test modules; sleep guard with
  a justified allowlist.

### S6.0 — Mount Align movement path on the real OnStep adapter (CRITICAL)

- **Allowed files:** `apps/collimation_tool/ui/mount_test_move_runner.py`,
  `apps/collimation_tool/ui/mount_test_move_panel.py` (movement dispatch/sizing only),
  `packages/astrotool_core/onstep/mount_pulse_adapter.py` (only if a capability must be *reported*,
  e.g. a `supports_timed_motion` flag — no new motion primitive); their tests; a new regression test
  driving the **production** `OnStepMountPulseAdapter` over the existing fake OnStep INDI client.
- **Behavior changed:** calibration bootstrap, manual RA/Dec nudges and screen moves use the adapter's
  angular move sized up front (AGENTS.md "Mount Align movement policy": ~25 % of frame, reference
  seeds) whenever timed pulses are unsupported; refusal reasons are surfaced, never masked by a
  fallback.
- **Tests first:** a regression that fails on current `main` (calibration / nudge against the real
  adapter + fake OnStep connection never moves the mount).
- **Frozen:** OnStepAdapter itself; `MountPort` protocol shape for other callers.
- **Dependencies:** none; blocks S3b and S6.6.
- **Non-goals:** extracting Mount Align orchestration (S6.6); `recenter_policy.py` and guide-tool
  pulse guiding (record as dependencies if they share the defect).
- **Proof:** failing-then-passing regression; proof manifest `proofs/46.yaml` (or linked); field-only
  assumptions listed.

### S4 — #51 hardware-boundary simulators

- **Allowed files:** `packages/astrotool_core/testing/` (extend existing fakes / add simulator modules,
  public via its `__init__`); new tests under `tests/core/testing/` and `tests/core/<sub>/` for simulator
  scenarios; `tests/conftest.py` tier table only; CONTRIBUTING.md new section "Reproducing a hardware
  bug locally" only; at most one minimal clock injection into `acquisition/stream_controller.py` /
  `FrameMailbox` if needed for deterministic frame timing (behaviour-neutral, default real clock).
- **Behavior changed:** none in production (test infrastructure only).
- **Consumes:** `astrotool_core.timing` (FakeClock) — never a second time model.
- **Scenarios (from #51):** OnStep mount (park/unpark, tracking with delayed transitions, stale
  properties, connect error variants, retryable/non-retryable rejects, stop); focuser (limits, stale
  metadata, backlash, Busy-forever, concurrent status reads); filter wheel (slots, invalid slot,
  Busy-forever/delayed completion, disconnect while moving, name variants); ToupTek camera (per-model
  capability matrix with E_NOTIMPL, exposure/gain metadata, frame timing, stale/motion-overlapping
  frames with explicit timestamps, FITS replay).
- **Proof:** ≥5 defect classes reproduced deterministically (unsupported capability, connect timeout,
  stuck Busy, OnStep compound-operation concurrency with deterministic scheduling, stale frames #43);
  one Mount Align and one autofocus component workflow on the simulators; the filter-wheel
  `TestSetSlot` wall-clock flake replaced by a deterministic scenario test (the old test may stay
  only if made deterministic); no live INDI/serial/SDK/network/real sleeps.
- **Frozen:** production code (except the optional stream clock injection); existing fakes' current
  behaviour for existing tests.
- **Non-goals:** S6.0c's lock semantics; fixing production defects found (record them).

### S6.0c — Stop during an angular GOTO

- **Allowed files:** `packages/astrotool_core/onstep/mount_pulse_adapter.py`,
  `packages/astrotool_core/onstep/connection.py` (lock semantics only),
  `apps/collimation_tool/ui/mount_test_move_runner.py` (abort path only),
  `apps/collimation_tool/ui/mount_test_move_panel.py` (Stop handler only),
  `apps/collimation_tool/ui/mount_park_panel.py` (status-poll path only); their tests.
- **First:** characterize with failing component tests: Stop pressed during a blocking `move_angular`
  must (a) not block the GUI thread beyond a bounded time, (b) actually stop the mount; park-panel
  status polls during a move must not block the GUI thread.
- **No assumptions:** read the installed OnStepAdapter 0.4.1 source to establish what abort/stop it
  offers while `move_*_axis_deg` blocks (thread-safety of calling stop concurrently, whether INDI abort
  is honoured) before choosing a design; cite it in the report.
- **Frozen:** the 9cea2e9 serialization guarantee (no interleaving of compound operations) must keep
  its tests green — stop is the deliberate exception and must be justified.
- **Dependencies:** S6.0 (done). Not S4 (uses the existing fake OnStep client).
- **Non-goals:** recenter_policy (S6.0b), runner clock migration (S3b).
- **Proof:** failing-then-passing tests; proof manifest draft; field-only assumptions.

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
| 2026-10-01 | S6.0 | 787ceed | Mount Align moves the production OnStep adapter: 21 component tests (realistic optics, rotation, inverted signs) all fail pre-fix (coordinator-reproduced) and pass now; no command < 30″; no partial screen moves; refusals unmasked. Field: confidence ~85–90 % (nudges/calibration), S6.0c Stop-freeze risk open |
| 2026-10-01 | S2 | (this commit) | 6 manifests (#49, #43, #47, filter stuck Busy, OnStep serialization, connect exception); 5 revert-proven in a separate worktree, 2 of them re-reproduced by the reviewer; CI fast job validates every manifest and rejects focused commands that could skip their tests; proof gaps recorded for S7 |
| 2026-10-01 | S3a | (this commit) | ~80 fake-time tests incl. before/at/after boundaries decided by production code; #13 tests deterministic (no threads, 10/10 under CPU load); 9087272 pause-before-start guard restored (mutation fails it); migrated modules 4.25 s → 1.9 s; sleep guard + import contract for `timing` |
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
- **2026-10-01, CI flake → S4 (#51).** `tests/core/filter_wheel/test_indi_filter_wheel_adapter.py::TestSetSlot::
  test_commands_the_device_and_reports_moving_then_arrived` failed once in the release-gate job
  (run 36784794838, commit 8149dd7 — docs only) and passed in the fast job of the same commit. It
  polls real wall time (`_wait_until`, 2 s) for a transient Busy that the FakeIndiServer can clear
  before the poll observes it. Belongs to the simulator's deterministic Busy/delayed-completion
  scenario (S4), driven by fake time (S3a).
- **S3a review → S6.3/S6.4.** Policy waits not caught by the sleep guard: wall-clock deadline
  polling with `time.monotonic()` in `focuser_panel.py:440`, `filter_wheel_panel.py:241`,
  `mount_park_panel.py:247`; `Event.wait(timeout)` used as a sleep (`stream_controller.py:164`).
  Uncancellable waits kept for neutrality: autofocus `settle_s`, searcher move-settle poll,
  motion-aware sample interval, tracking settle poll — make interruptible in S6.3/S6.4.
  `TrackingEnforcer` (`mount/operating_mode.py`) needs a `clock=` pass-through (S6.4).
  `StreamController`/`FrameMailbox` need the clock before a threaded GuideController can run on fake time (S4).
- **S6.0 findings (2026-10-01).** (1) `recenter_policy.py` reacquisition hits the same refusal → S6.0b.
  (2) GuideTool pulse guiding (`correction_policy.py:25`, `calibration_controller.py:52`) relies only on
  `pulse_axis`; dormant because `guide_tool/main.py` wires no mount (audit P03) — must be solved before a
  mount is wired into GuideTool. (3) OnStepAdapter's 30″ minimum is not exposed as a capability
  (`MountCapabilities`, S6.5). (4) Nudges still capture BEFORE/AFTER measurement frames, against AGENTS.md
  manual-move rule → S6.6/S6.7. (5) Status texts on angular-only mounts still mention rate presets
  (`_no_motion_text`, `_exclude_camera_bounded`) → S6.6. (6) Pre-existing on timed-capable mounts:
  calibrated nudges solve at the calibration preset but execute at nudge `rate_preset` "7" — possible
  ~2.4× over-move (candidate explanation for diagnostic de295656) → needs its own failing regression (S6.6).
- **S2 gaps → S7.** (a) FocuserPanel half of 3fc09ae (stuck-moving re-enables In/Out after timeout) has
  no test — all 23 focuser-panel tests pass with the fix reverted. (b) #43's static-scene test passes
  with the #43 guards reverted (older degenerate-axis check catches it) — a test that isolates the #43
  guard is missing. (c) #43 has no captured frame dataset in `datasets/regressions/`. (d) #49 recovery
  tests take ~80 s of wall clock → fake time in S3b. (e) The `registry.py` EFW fallback literal has no
  test (→ S6.1). (f) `changed_tests.py` maps `proofs/**`/`scripts/prove.py` to ALL_FAST instead of the
  two manifest test modules (#50 follow-up). (g) No CI rule yet *requires* a manifest on a bug-fix PR.
- **S6.0 re-review (APPROVE) leftovers.** (a) The angular floor reaches the panel via a duck-typed
  `min_angular_arcsec` property read with `getattr(..., 0.0)` — fold into `MountCapabilities` with the
  other `hasattr` checks (audit C02) in S6.5. (b) Small up/down screen moves are always refused on the real
  main camera (5% of 440″ = 22″ < 30″) — disable/annotate the button in S6.6. (c) A two-axis screen move
  can still stop halfway if the second component is refused for a non-floor reason (hard limit, time not
  trusted, connection) and "Move failed" doesn't say one axis already moved — S6.6. (d) S6.0c (Stop/
  status polls blocking the GUI thread during a GOTO) is the main field risk for the next Pi run.
- **S5 live contradictions (9)** are listed in `docs/quality/duplication-audit.md`; each is owned by an
  S6 step and gets a failing regression before its fix.
