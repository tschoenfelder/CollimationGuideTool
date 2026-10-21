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
| S3b | #53 | Migrate `mount_test_move_runner.py`, `recenter_policy.py` (after S6.0); panel wall-clock polling; interruptible waits | issue agent | APPROVE-WITH-FIXES (xfail raises, guard gaps) → fixed | done | c55b9c6 | `python scripts/prove.py 53-part-b` |
| S4 | #51 | Scenario simulators (OnStep mount, focuser, filter wheel, ToupTek camera) on FakeClock; 5 defect reproductions | issue agent | 2 rounds: APPROVE-WITH-FIXES (fidelity) → APPROVE-WITH-FIXES (2 minor) → fixed | done | 0fdf781 | `python scripts/prove.py 51` |
| S5 | #55, #52 | Duplication inventory + size/dependency baseline (`docs/quality/duplication-audit.md`) | analysis agent | coordinator spot-check (P01 confirmed in code) | done | (this commit) | docs only |
| S6.0 | #46, #31, #55 (P01/P02) | **CRITICAL, promoted:** Mount Align bootstrap/nudge/screen moves go through `pulse_axis`, which production `OnStepMountPulseAdapter` always refuses; no rate is ever installed, so angular moves never start on the rig. Size the first move angularly from the optics seeds instead of a timed bootstrap; failing regression against the real adapter + fake OnStep connection first | issue agent | 2 rounds: APPROVE-WITH-FIXES → APPROVE | done | 787ceed | `python scripts/prove.py 46-mount-align-angular-path` |
| S6.0b | #39, P01 | Guide-assisted reacquisition (`recenter_policy.py:113`) calls `pulse_axis` on the production OnStep adapter → always `pulse_rejected`, adapter message dropped; move to the angular path (after S6.0). **Must use the same ms↔arcsec factor S6.0 stores the matrix in (equivalent ms at `calibration_center_rate_x`·sidereal) — that factor needs one public owner first** | issue agent | 4 rounds: APPROVE-WITH-FIXES → REJECT (unequal scales) → APPROVE-WITH-FIXES (tracker margin) → fixed | done | f24a523 | `python scripts/prove.py 39-reacquisition-angular` |
| S6.0c | #49, AGENTS manual-move rule | `OnStepMountPulseAdapter.move_angular` holds `connection.operation_lock` for the whole blocking GOTO (up to 30 s); `abort()` takes the same lock on the GUI thread before `stop()`, and park-panel status polls also take it → Stop would freeze the UI and not cancel; `_cancel` never checked. Characterize with a failing component test, then decide stop/lock semantics (verify OnStepAdapter's real abort semantics — no assumptions) | issue agent | 3 rounds: APPROVE-WITH-FIXES (C1/C2 #44 regressions, C3) → APPROVE-WITH-FIXES (R1) → fixed | done | 52fd72f | `python scripts/prove.py 49-stop-during-angular-goto` |
| S6.0d | #39, user decision | Astronomical-mode reacquisition via OnStepAdapter 0.5.0 `guide_pulse` (tracking preserved): capability-detected (main stays pinned to published 0.4.1 → clear 'needs OnStepAdapter ≥ 0.5.0' message), separate guide-pulse calibration (guide rate is not exposed), simulator modelled on the 0.5.0 source, distinct capability so Mount Align's timed/angular choice is unaffected; pin bump only after 0.5.0 is published and the supervised physical acceptance passed | issue agent | APPROVE-WITH-FIXES (Stop/tracking-off reporting, probes, mode re-read) → fixed | done | feb46e5 | `python scripts/prove.py 39-astro-guide-pulse` |
| S6.0e | #39, S6.0d | OnStepAdapter 0.5.0 published 2026-10-06 (release v0.5.0; wheel sha256 2420ba1c…). **Correction (S6.0e):** the published wheel is NOT the pre-release build S6.0d was checked against — it was rebuilt after OnStepAdapter d7c5284 (post-chunk wait for the guide flag `G` to clear, refusal/timeout there emergency-stops); the S6.0d simulator differed in 494/3150 cases and was updated to 0/3150; a permanent contract test now replays the fake against the installed controller. Bump the pyproject pin 0.4.1 → 0.5.0, make the real capability path the default in tests (keep the 0.4.1-absence case simulated), contract-check the production adapter against the installed 0.5.0, update install.md; Pi venv upgrade happens at the S8 rollout | issue agent | APPROVE-WITH-FIXES (real-facade contract test, Pi reinstall steps) → fixed | done | 23a9cb2 | `python scripts/prove.py 39-astro-guide-pulse` |
| S6.1 | #55 | Single-source device defaults + config-source contract test | issue agent | APPROVE-WITH-FIXES (diagnostics call-time, scanner blind spots) → fixed | done | 37db9f0, 61d1e27 | `python scripts/prove.py 55-single-source-defaults` |
| S6.2 | #52 | `DeviceConnectionService` (Focuser → MountPark → MountTestMove) | issue agent | APPROVE-WITH-FIXES (internal-error masking, contract scan, restore, prove.py batching) → fixed | done | 80220a6 | `python scripts/prove.py 52-device-connection-service` |
| S6.3 | #52 | `OperationLifecycle` + bounded Busy timeout (FilterWheel, Focuser) | issue agent | APPROVE-WITH-FIXES (Alert grace, lifecycle on_error, settle log) → fixed | done | b22b871 | `python scripts/prove.py 52-operation-lifecycle` |
| S6.4 | #48 | Remove Mount Align **and autofocus** local Star/Terrestrial toggles; derive from global OperatingMode (M04 decided). Also (from S6.0c review P-a): switching to Terrestrial while the mount is busy stores the mode but the gate stays BLOCKED until the next measurement gate — add a one-shot re-enforce when the busy period ends | issue agent | 3 rounds: APPROVE-WITH-FIXES (+2 user decisions) → APPROVE → hardening | done | be7353e | `python scripts/prove.py 48-one-mode-source` |
| S6.5 | #55 | ToupTek capability table (unsupported features contract-driven) | issue agent | APPROVE-WITH-FIXES (cooler-left-on safety, range retry) → APPROVE | done | 5f38a4c | `python scripts/prove.py 55-capability-tables` |
| S6.5a | production defect (found by S4 review) | `touptek_adapter.py:54` `_FLAG_MONO = 0x40` is the SDK's `TOUPCAM_FLAG_USB30`; real `TOUPCAM_FLAG_MONO` is `0x10` (resources/touptek/toupcam.py:24/26). `is_color_sensor()` (line 639) therefore answers "not USB3": a colour USB3 camera would be treated as mono (no debayer). Option fallbacks also wrong (RGB 0x16→0x0c, FLUSH 0x36→0x3d, NOFRAME_TIMEOUT 0x3F→0x01, AUTOEXPO_TRIGGER 0x5A→0x51) — only used when the SDK lacks the name. Failing regression first (S4 leaves a strict xfail), then fix; check which rig cameras are affected | issue agent | APPROVE-WITH-FIXES → pairing gap closed structurally | done | 4864cc8 | `python scripts/prove.py 51-touptek` |
| S6.5b | production defect (found by S6.5a) | `touptek_adapter._prepare_capture_mode` puts `TOUPCAM_OPTION_NOFRAME_TIMEOUT = 1` (1 ms); the SDK requires ≥ `TOUPCAM_NOFRAME_TIMEOUT_MIN` = 500 ms (`resources/touptek/toupcam.py:109`, `:682`) → either rejected (log warning) or a 1 ms no-frame timeout on the rig. Characterize on the simulator, decide the intended value from the SDK docs, fix; add a connect-time log of `model.flag` so the S6.5a camera-flag inferences become known on the next Pi run | issue agent | APPROVE-WITH-FIXES → fixed | done | 973d0ab | `python scripts/prove.py 51-touptek-noframe` |
| S6.6 | #52, #43, #31, #55 | Mount Align orchestration out of `mount_test_move_panel.py` into application layer: S6.6a neutral extraction (10 Qt-free modules); S6.6b behaviour follow-ups (nudge returns immediately with background #43 check, one artificial-star owner, `MountCapabilities` instead of probes, `onstep/errors.py`, GUI-thread exposure snapshot, screen-move floor decision) | issue agent | S6.6a APPROVE (byte-identical differential, 13 scenarios); S6.6b APPROVE-WITH-FIXES (C1 abandoned check sent tracking commands, C2, C4, C5) → fixed, revert-proven | done | 6b6d899, 1a89844 | `python scripts/prove.py 52-nudge-returns` · `52-mount-align-owners` · `52-exposure-snapshot` · `55-capabilities-errors-defaults` · `31-calibrated-move-rate` · `31-manual-move-texts` |
| S6.7 | #52 | Fresh-frame-after-motion service shared by Mount Align + autofocus | issue agent | APPROVE-WITH-FIXES (Stop/cap, stale nudge reference, trigger-time stamp) → fixed | done | 9fc3fa2 | `python scripts/prove.py 52-fresh-frame-after-motion` |
| S6.8 | #55, #31, #46 | Movement-path analysis (`docs/quality/movement-paths.md`, 25 paths, 13 findings) | analysis agent | coordinator spot-check | done | 1534d6d | docs only |
| S6.8a | #55, #31, #46, #44, #49 | One mount-motion executor (`application/mount_motion.py`): one motion in flight app-wide (runner, reacquisition, park/unpark), owner-scoped lock-free abort, interruptible settle; M03 removed (no per-move emergency stop); Terrestrial+tracking ON → enforcer repair (trail entry + operator note) then one re-check, else recovery text; Astronomical+tracking ON → explicit refusal until S6.8b; fine-collimation Cancel stops the mount; quit aborts first; auto-unpark enforces #44 | issue agent | APPROVE-WITH-FIXES (Terrestrial dead end, abort race, e2e Cancel test, trail flooding) → fixed, revert-proven | done | c577fd4, 85f6bca, 497459a | `python scripts/prove.py 55-one-mount-motion-executor` |
| S6.8b | #31, #46, #39, user decision | Astronomical Mount Align moves with 0.5.0 guide pulses while tracking (calibration/nudge/screen move sized in guide-ms, chunked) through the S6.8a executor | | | todo | | |
| S6.8c | #49, #55 | Focuser: S6.0c pattern (lock-free Stop, jog off the GUI thread) (R7); global "Stop mount" button enabled while any mount motion is in flight, park slew included, via `executor.abort_in_flight()`; pulse adapter `abort()` stops through the shared connection; park/unpark report "stopped by the user" (errors kept) (R5) | issue agent | R7: APPROVE-WITH-FIXES → fixed; R5: coordinator rework (adapter, not MainWindow borrow) + APPROVE-WITH-FIXES (surviving mutant, hidden errors, check-then-act) → fixed, mutation-proven | done | e028cc5, 436b312, ff6754e, f00007f, 9eb03dd | `python scripts/prove.py 49-focuser-stop` · `55-global-mount-stop` |
| S6.8d | #55 (P04/P05) | Delete dead timed-motion path + settings (R9, with config-migration note), explicit nudge cap (F11), stale texts + boundary token/import contract (R11) | | | todo | | |
| S7 | #15 #21 #29–#33 #35 #37 #39 #43 #44 #46 #49 | Proof manifests + missing low-tier tests; field-only assumptions listed | issue agent | coordinator verification (worktree, all proofs PASS) | done | 5f7c689 | `python scripts/prove.py <issue>` |
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
- **Contract extensions (coordinator, 2026-10-01/02):** D1 — `onstep/mount_park_adapter.py` and
  `onstep/focuser_adapter.py` status reads via `try_operation()` (same goal: a Stop click must reach the
  GUI thread during a GOTO); `testing/sim_onstep.py` stop model; after review (#44 safety): one
  freshness field on the park status type + decision logic in `mount/tracking_mode.py` /
  `mount/operating_mode.py` and the minimal callers surfacing the fail-closed message.
  `mount_park_panel.py` ended up unchanged.
- **Non-goals:** recenter_policy (S6.0b), runner clock migration (S3b).
- **Proof:** failing-then-passing tests; proof manifest draft; field-only assumptions.

### S3b — #53 time abstraction (second half)

- **Allowed files:** `apps/collimation_tool/ui/mount_test_move_runner.py`,
  `apps/collimation_tool/application/recenter_policy.py` (timing only — its `pulse_axis` move path is
  S6.0b), the wall-clock confirmation timeouts in `focuser_panel.py` / `filter_wheel_panel.py` /
  `mount_park_panel.py` (timing only), their tests, `tests/core/testing/test_no_policy_sleep.py`
  (allowlist shrinks; extend detection to `time.monotonic()` deadline polling and `Event.wait` used as a
  sleep in policy modules if feasible), `tests/conftest.py` tier table only.
- **Behavior changed:** none (clock is an optional keyword, default real clock).
- **Tests:** fake-time tests for runner retry/unpark/park polling/settle, deadline boundaries
  before/at/after, cancellation in each wait state; the slow Mount Align tests
  (`test_calibration_failure_recovery.py` ~80 s, `test_calibration_sizing.py` ~5.5 s/test,
  `TestMountTestMovePanel` ~8.5 min) moved to fake time where the runner was the cause — measure before/after.
- **Frozen:** S6.0/S6.0c behaviour and tests; no hardware op on the Qt main thread.
- **Dependencies:** S3a, S6.0, S6.0c (done). Blocks S6.0b (recenter_policy) and S6.6.
- **Non-goals:** recenter_policy's angular move path (S6.0b); interruptible waits beyond what the clock
  migration gives for free (S6.3).
- **Proof:** allowlist entries for the runner and recenter_policy deleted; wall-time before/after; ≥15
  new fake-time tests; manifest for #53 (part B) and #13 if touched.

### S6.5a — ToupTek mono flag / SDK constants (production defect)

- **Allowed files:** `packages/astrotool_core/camera/touptek_adapter.py` (constants and their use),
  its tests, `tests/core/camera/test_touptek_adapter_simulated.py` (flip the two strict xfails).
- **Behavior changed:** `is_color_sensor()` uses the SDK's real `TOUPCAM_FLAG_MONO` (0x10), looked up by
  name from the SDK module like the options (single source = the SDK), with fallbacks equal to the
  vendored SDK values; wrong option fallbacks corrected.
- **First:** the two strict xfails are the failing regression; also check every other `_FLAG_*` /
  `_OPTION_*` / event constant in the adapter against `resources/touptek/toupcam.py`.
- **Investigate & report (no assumptions):** which real rig cameras were mis-classified before (model
  flags from the vendored SDK's model table if present, else say unknown) and what downstream path
  (debayer / colour handling) that affected; link to the open guide-camera colour/star-detection issue if
  relevant.
- **Frozen:** capture pipeline behaviour for correctly-classified cameras.
- **Non-goals:** the S6.5 capability table refactor.
- **Proof:** xfails flip to passing tests; manifest.

### S6.0b — guide-assisted reacquisition on the angular path

- **Allowed files:** `apps/collimation_tool/application/recenter_policy.py`, its callers' wiring only where
  the mount/calibration hand-off must change (`star_acquisition.py`), the ms↔arcsec factor's single public
  owner (move the S6.0 "equivalent ms at `calibration_center_rate_x`·sidereal" conversion out of
  `mount_test_move_panel.py` into one public function in the application/domain layer and make the panel
  use it — that panel change is the only one allowed there), their tests.
- **Behavior changed:** reacquisition moves the production OnStep mount via `move_angular` (sized from the
  Guide calibration matrix through the single conversion owner, respecting the adapter's floor), never
  `pulse_axis` on a mount without timed pulses; refusal messages surfaced, not dropped.
- **Tests first:** fails on HEAD against the production adapter over the #51 OnStep simulator.
- **Frozen:** timed-capable mounts keep their behaviour; S6.0/S6.0c tests green.
- **Non-goals:** GuideTool pulse guiding (dormant, no mount wired — recorded dependency); Mount Align
  extraction (S6.6).
- **Proof:** failing-then-passing tests; manifest.

### S6.5b — ToupTek no-frame timeout + flag log

- **Allowed files:** `packages/astrotool_core/camera/touptek_adapter.py`, its tests,
  `packages/astrotool_core/testing/sim_touptek.py` (only if the SDK's NOFRAME_TIMEOUT rule must be modelled).
- **Behavior changed:** `_prepare_capture_mode` sets a valid NOFRAME_TIMEOUT per the vendored SDK
  (0 = disable or ≥ `TOUPCAM_NOFRAME_TIMEOUT_MIN`) — choose from the SDK docs and the adapter's own
  no-frame handling, justify; one INFO log line at connect with model name, raw `model.flag` (hex) and the
  derived mono/colour/USB3/TEC classification.
- **Tests first:** the simulator rejects out-of-range values like the SDK documents (cite) → failing test.
- **Non-goals:** S6.5 capability-table refactor.
- **Proof:** failing-then-passing test; manifest; field item: read the flag log on the next Pi run.

### S6.0d — astronomical reacquisition via OnStepAdapter 0.5.0 `guide_pulse`

- **Allowed files:** `packages/astrotool_core/onstep/mount_pulse_adapter.py` (+ onstep `__init__`),
  `packages/astrotool_core/mount/port.py` (one NEW, distinct capability for tracking-preserving guide
  pulses — do NOT change what `supports_pulse_guiding` means to Mount Align), `testing/sim_onstep.py` +
  `testing/fake_onstep_indi_client.py` (model 0.5.0's guide_pulse from its source), the reacquisition path
  (`application/recenter_policy.py`, `application/star_acquisition.py`, a new application module for the
  guide-pulse calibration if needed), the minimal UI binding to show the new outcome/message, their tests.
- **Source of truth:** the unpublished wheel
  `C:/Users/tscho/Documents/Codex/CameraTest/OnStepAdapter/dist/onstep_adapter-0.5.0-py3-none-any.whl`
  (read `indi_guiding.py`, `indi_mount.py`, `indi_client.py`; cite lines). Do NOT install it into `.venv`
  and do NOT change the pyproject pin (main stays on published 0.4.1). An optional contract check against
  the real 0.5.0 API may run in a throwaway venv outside the repo.
- **Behavior:** in Astronomical mode with tracking ON, reacquisition uses `mount.guide_pulse(direction,
  ms)` when the installed OnStepAdapter provides it (capability-detected once, single owner); otherwise it
  refuses up front with "astronomical reacquisition needs OnStepAdapter ≥ 0.5.0 (guide pulses while
  tracking)". Terrestrial/tracking-off keeps the S6.0b angular path. Never stops/starts tracking itself.
  Guide pulses are 20–5000 ms (0.5.0 bounds); the guide rate is not exposed → derive px-per-guide-ms from
  a small, bounded guide-pulse calibration (per direction, measured on fresh frames) or another
  evidence-based approach — justify. Surface 0.5.0's result fields (refusal reason, `tracking_preserved`,
  warnings); if a failure after an issued chunk triggered 0.5.0's emergency_stop (tracking OFF), report
  that prominently.
- **Tests first:** against the production adapter + simulator modelling 0.5.0 exactly (preflight
  blockers, chunking, lock, emergency_stop on failure).
- **Frozen:** Mount Align's timed/angular selection; S6.0/S6.0b/S6.0c behaviour; #44 gate.
- **Non-goals:** GuideTool pulse guiding; publishing/pinning 0.5.0; field acceptance (user-supervised).
- **Proof:** failing-then-passing tests; manifest; field items (supervised first physical guide pulse is
  the user's, before the pin bump).

### S6.1 — single-source device defaults (#55 D01–D03)

- **Allowed files:** configuration/registry/default resolution (`packages/astrotool_core/config/**`,
  the registry module, `filter_wheel/indi_filter_wheel_adapter.py` default only,
  `testing/fake_indi_server.py` default only), consumers that hard-code the same literals (INDI host/port,
  EFW device name, config path constants) — only the literal → owner replacement; `tests/conftest.py`
  (the config-path patching, if it can collapse to one owner); their tests; `templates/` if a config default
  is documented there.
- **Behavior:** none (behaviour-neutral) except fixing live contradictions found by S5 (e.g. FakeIndiServer
  defaulting to the wrong "ToupTek EFW 1", the adapter's "Filter Wheel" default) — each such fix gets a
  failing test first.
- **Tests:** a config-source contract test that fails if a second contradictory literal appears (scan
  production code for the owned literals); injecting an alternate config proves every consumer sees it.
- **Frozen:** the external config file format (`~/.CollimationGuideTool/config.toml`), public signatures.
- **Proof:** duplication-audit D01–D03 items resolved (cite); manifest.

### S6.4 — #48 one mode source (Mount Align + autofocus)

- **Allowed files:** `apps/collimation_tool/ui/mount_test_move_panel.py` (the local Star/Terrestrial
  toggle and everything that reads it — nothing else in that file), the autofocus panel/controller where
  its local Star/Terrestrial selector lives, `packages/astrotool_core/mount/operating_mode.py`
  (`TrackingEnforcer`: clock pass-through, one-shot re-enforce when a busy period ends),
  `apps/collimation_tool/ui/main_window.py` (mode wiring only), their tests; `install.md` if the UI text
  is documented there.
- **Behavior changed (#48 + user decision M04):** the global OperatingMode is the single source;
  Mount Align's measurement strategy and autofocus's metric (Terrestrial → Tenengrad, Astronomical →
  star/FWHM) derive from it; changing the global mode updates both; Natural vs Artificial Star stays
  (orthogonal target type, AGENTS.md). Switching to Terrestrial while the mount is busy re-enforces once
  the busy period ends (S6.0c review P-a). Tracking-settle poll becomes interruptible by Stop if it falls
  out naturally — otherwise record for S6.3.
- **Tests first:** #48 acceptance (no local selector; contradictory combinations impossible; mode change
  propagates), M04, P-a.
- **Frozen:** #44 fail-closed gate semantics (S6.0c), S6.0b/S6.0d reacquisition decisions.
- **Non-goals:** extracting Mount Align orchestration (S6.6).
- **Proof:** failing-then-passing tests; manifest for #48.

### S6.5 — capability tables (camera + mount)

- **Allowed files:** `packages/astrotool_core/camera/touptek_adapter.py` (unsupported-feature handling
  becomes table/contract-driven per the S5 audit C-items: one capability decision per feature, no ad-hoc
  E_NOTIMPL try/except per call site), `packages/astrotool_core/camera/port.py` (camera capabilities type
  if needed), `packages/astrotool_core/mount/port.py` (`MountCapabilities` gains the angular floor/maximum
  and replaces the duck-typed `min_angular_arcsec` / `hasattr` capability probes — audit C02 — keep the
  old attributes as deprecated aliases if callers outside your files use them),
  `packages/astrotool_core/onstep/mount_pulse_adapter.py` (report them), D04 cooling default single owner
  (`config/camera_settings.py`, `ui/camera_panel.py` default only, touptek adapter default),
  `testing/sim_touptek.py`, their tests.
- **Behavior changed:** none for supported features; unsupported features are reported once via the
  capability table (no repeated SDK calls/log spam).
- **Tests:** capability contract per simulated model (G3M678M, ATR585M, GPCMOS02000KPA, synthetic);
  config-source contract covers the cooling default.
- **Frozen:** S6.5a/S6.5b constants table; S6.0–S6.0d mount behaviour.
- **Non-goals:** Mount Align/reacquisition callers' migration beyond reading the new capability fields
  where they currently duck-type (record the rest for S6.6).
- **Proof:** failing-then-passing tests; manifest.

### S6.0e — pin OnStepAdapter 0.5.0

- **Allowed files:** `pyproject.toml` (the onstep-adapter dependency URL only → the published v0.5.0
  wheel), `install.md` (version notes / Pi upgrade instructions), the `.venv` (reinstall), tests that
  assumed 0.4.1 (`install_onstep_adapter_050_exports` and the capability-detection tests in
  `tests/core/onstep/`, `tests/collimation/application/test_guide_pulse_reacquisition.py`, simulators in
  `testing/sim_onstep.py` / `fake_onstep_indi_client.py` only where they hard-code the 0.4.1 world),
  their contract tests; proofs/39-astro-guide-pulse.toml (field-only notes).
- **Behavior changed:** production now runs on 0.5.0, so the astronomical guide-pulse path is live; the
  0.4.1-absence path stays tested via simulation (an installed package without `guide_pulse`).
- **Must verify (no assumptions):** the installed package's version and `guide_pulse` facade; its
  `indi_status`/`indi_meridian`/`indi_axis_motion`/`indi_tracking`/`indi_stop`/`indi_home` vs 0.4.1 — list
  every behavioural difference relevant to CGT (diff the two wheels) and re-run the S6.0–S6.0d suites
  against it; the sim-vs-real comparison script from the S6.0d review against the INSTALLED package.
- **Non-goals:** Pi venv upgrade (S8 rollout).
- **Proof:** CI green on 0.5.0; manifest note; field item: Pi venv upgrade + the user-supervised guide
  pulse (if not already done before the release).

### S6.2 — DeviceConnectionService (#52 K-items)

- **Allowed files:** new application service module(s) under `apps/collimation_tool/application/` (or
  `astrotool_core` if GuideTool needs it too — justify), `ui/focuser_panel.py`, `ui/filter_wheel_panel.py`,
  `ui/mount_park_panel.py`, `ui/camera_panel.py` (connect/disconnect/stream-start paths only),
  `ui/main_window.py` (closeEvent per-panel isolation; moving the S6.4 P-a 500 ms re-enforce timer into
  the service's poll), `apps/guide_tool/ui/**` connect path (K03: only `ConnectionError` caught; previous
  camera never released), `packages/astrotool_core/onstep/connection.py` (KeyError from 0.4.x/0.5.0
  `connect()` not caught by `acquire()`), their tests. **Not** `mount_test_move_panel.py` (S6.6/S6.7 own it).
- **Behavior:** one connect/disconnect lifecycle with normalized failure reporting (exception classes,
  log line, `last_connect_error`) and guaranteed cleanup; FilterWheelPanel gets the ca5bb92 fix it missed;
  camera stream start guards connect; GuideTool releases the previous camera (#45 class) and catches the
  same failure set; disconnect/stop guarded; closeEvent isolates per-panel failures.
- **Tests first:** failure injected at every phase (connect/start/wait/finish/cancel/disconnect) leaves
  each panel retryable; simulators (#51) + fake clock; thin panel binding tests.
- **Proof:** failing-then-passing tests; audit K01–K03 resolved; manifest.

### S6.7 — fresh-frame-after-motion service (ROLLOUT BLOCKER)

- **Allowed files:** new shared service (application or `astrotool_core.acquisition`) built on
  `acquire_stable_frame` / `acquire_settled_frames`; callers: `application/star_acquisition.py` /
  `recenter_policy.py` reacquisition measurement (replace the latest-frame-after-settle fallback and
  `GUIDE_FRESH_FRAME_TIMEOUT_S`), `application/autofocus_controller.py` (T05: fixed 5 s fresh-frame wait
  vs exposure-scaled), `ui/mount_test_move_panel.py` capture timing only (exposure-scaled timeouts,
  completed_at / reference max age on the injected clock — S3b(c)); nudges: manual moves must not wait for
  BEFORE/AFTER measurement frames (AGENTS.md) — decouple if contained; their tests.
- **Behavior:** every measurement after motion uses a frame whose exposure STARTED after the motion
  ended (+ settle), exposure-scaled deadline, explicit timeout outcome; one owner of the rule.
- **Tests first:** stale/motion-overlapping frames via `testing.sim_frames.FrameTimeline` (#51) for
  reacquisition, autofocus, Mount Align; long exposures (>2 s) no longer time out.
- **Frozen:** S6.0b/S6.0d/S6.4 decisions; #49 off-GUI-thread waits.
- **Proof:** failing-then-passing tests; manifest; TestMountTestMovePanel wall time before/after.

### S6.3 — OperationLifecycle + interruptible waits (#52 L-items, #53 leftovers)

- **Allowed files:** a small application-level operation-lifecycle helper (idle/running/cancelled/failed/
  succeeded, guaranteed cleanup), the runners (`ui/autofocus_runner.py`, `ui/fine_collimation_runner.py`,
  FovCalibrator, `ui/mount_test_move_runner.py` waits/retry only), `focuser_panel.py` /
  `filter_wheel_panel.py` / `mount_park_panel.py` (Busy/confirmation timeout + periodic status-poll
  guarding only), `filter_wheel/indi_filter_wheel_adapter.py` (S4 findings: disconnect-while-moving
  reported as Busy forever; out-of-range slot Alert invisible; client left open if post-connect probe
  raises; refresh throttle on the real clock), `onstep/focuser_adapter.py` + `onstep/mount_pulse_adapter.py`
  (K02 adapter side: one owner of the OnStep exception set), `mount/operating_mode.py` / `tracking_mode.py`
  (settle poll interruptible), `astrotool_core/acquisition/stream_controller.py` (clock seam; cadence
  `Event.wait`), their tests; `tests/core/testing/test_no_policy_sleep.py` allowlist shrink.
- **Behavior:** exceptions in any runner leave it idle and retryable (L01/L02: AutofocusRunner,
  FineCollimationRunner, FovCalibrator lack try/finally); MountParkPanel stuck-Busy elif ordering (L02);
  autofocus search treats not-yet-Busy focuser as settled (L-item) — verify & fix; Stop interrupts waits in
  progress (runner fresh-park/unpark/gate/retry/settle/re-park; tracking settle poll; autofocus settle);
  `_pulse_with_retry` honours Stop (flip the S3b strict xfail); panel polls never raise into Qt.
- **Tests first:** phase-injection per runner/panel/adapter on the #51 simulators + FakeClock.
- **Frozen:** S6.0c stop/lock semantics, S6.2 lifecycle service, S6.4 enforcer semantics, S6.7 rule.
- **Non-goals:** Mount Align panel orchestration (S6.6).
- **Proof:** failing-then-passing tests; manifest; sleep-guard allowlist shrinks.

### S7 — proof manifests for the open HIGH issues

- **Allowed files:** `proofs/*.toml` (new), new test modules only (no production changes), `tests/conftest.py`
  tier table only.
- **Scope:** #15, #21, #29, #30, #31, #32, #33, #35, #37, #39 (beyond 39-*), #43, #44, #46, #49 — one
  manifest each (unless an existing manifest already covers it — then extend/link), mapping every AC in
  `gh issue view <n>` to tests; add the missing lowest-tier tests where an AC has only high-level or no
  proof; list field-only assumptions honestly. Gaps listed in earlier manifests (S2: FocuserPanel half of
  3fc09ae, #43 guard isolation, #43 dataset) are closed here where possible with tests.
- **Non-goals:** changing production code (record defects found as dependencies).
- **Proof:** every listed issue has a manifest; `prove.py --validate-all` green; an AC→test table per
  issue in the report.

### S6.6a — Mount Align orchestration out of the panel (behaviour-neutral)

- **Allowed files:** `apps/collimation_tool/ui/mount_test_move_panel.py` (shrinks to UI binding),
  new modules under `apps/collimation_tool/application/` (e.g. calibration state machine, capture
  validity / measurement, failure classification, move sizing/planning, nudge/screen-move planning — split
  by responsibility per `docs/quality/duplication-audit.md` "mount_test_move_panel.py responsibility map";
  no mega-controller), their new tests; existing Mount Align tests only where an import path must change.
- **Behaviour:** none. Characterization first: every behaviour moved must be pinned by an existing or new
  test that passes before AND after (run the whole Mount Align suite + TestMountTestMovePanel unchanged).
- **Frozen:** all S6.0–S6.7 behaviour, messages, diagnostics keys, the public panel API used by
  MainWindow/tests.
- **Proof:** panel size/function metrics before/after; application modules have direct component tests that
  don't instantiate MainWindow or the panel; manifest (#52).

### S6.6b — Mount Align behaviour follow-ups (after S6.6a)

**Contract (2026-10-12).** Allowed: `ui/mount_test_move_panel.py` + `application/mount_align_*.py`,
`application/star_acquisition.py` / `recenter_policy.py` (capability + cancel wiring only),
`ui/main_window.py` + `ui/focuser_panel.py` (exposure snapshot wiring only), `onstep/` (new `errors.py`),
`testing/fake_*` doubles, D04 pinned doubles, their tests, `tests/contracts/*` pins. NOT the runner's
motion/tracking logic and not a new motion executor (that is the S6.8 fix). Every behaviour change gets a
failing test first; manifest per sub-item group.
Collected from the tracker: user nudge decision (return immediately, #43 check in background); M02 (one
tracking-verification path); M05 (one artificial-star owner; `_calibration_distance_m`); duck-typed
capability callers → `MountCapabilities`; `TrackingEnforcer`/`ensure_tracking_mode` `cancel=` wired from
Stop; GUI-thread exposure snapshot as `exposure_hint` (reacquisition + autofocus); K01 connect handler →
S6.2 service (remove the contract-test pin); `ONSTEP_OPERATION_ERRORS` → neutral `onstep/errors.py`
(+ `fake_onstep_indi_client` tuple); D04 pinned test doubles; stale rate-preset texts on angular-only
mounts; Small up/down screen move always refused on the real main camera (disable/annotate); two-axis
screen move non-floor refusal after the first axis moved (message); timed-mount nudge preset over-move
(~2.4×, failing regression first); simulator workflow tests location. Contract written when S6.6a lands.

### S6.8 — no competing movement path (analysis first)

- **Allowed:** read-only analysis report `docs/quality/movement-paths.md` (new) while S6.6a runs; fixes
  only after S6.6a lands (separate contract).
- **Scope:** every code path that moves the mount (RA/Dec) or focuser — Mount Align calibration/nudge/
  screen move/abort return, reacquisition (angular + guide pulse), runner pulse/timed fallback, GuideTool
  correction policy (dormant), M03 (`MountTestMoveRunner` turns tracking off before every move regardless
  of mode), any raw INDI access — mapped against AGENTS.md "Mount movement boundary"; flag duplicates,
  bypasses, and #31/#46 competing paths; recommend fixes.

### S6.8a — one mount-motion executor (after S6.6b)

**Contract (2026-10-13).**
- **Allowed:** new `apps/collimation_tool/application/mount_motion.py`; `ui/mount_test_move_runner.py`,
  `application/recenter_policy.py`, `application/star_acquisition.py`, `application/guide_pulse_reacquisition.py`
  (caller side only), `application/mount_align_*.py`, `ui/mount_test_move_panel.py`, `ui/main_window.py`
  (wiring + closeEvent order), `ui/fine_collimation_panel.py` (Cancel wiring), `ui/mount_park_panel.py`
  (park/unpark register with the executor's in-flight tracking — no new Stop button, that is S6.8c),
  `astrotool_core/mount/operating_mode.py` / `tracking_mode.py` (only if the "may a move stop tracking"
  rule needs an enforcer method), `testing/` doubles and simulators, their tests, new manifest(s).
- **Behaviour changed:** (1) every RA/Dec motion (Mount Align runner, reacquisition angular + guide pulse,
  park/unpark) goes through one executor that allows **one in-flight mount motion app-wide** (a second
  request is refused with a clear busy reason, never queued silently); (2) the runner no longer calls
  `stop_tracking()` before a move (M03/F1): Terrestrial relies on the enforcer (OFF), a tracking mount is
  reported, not emergency-stopped; **Astronomical + tracking ON: Mount Align angular motion is refused
  with an explicit message until S6.8b** (no silent tracking loss); (3) settle and the steps of a 2-axis plan
  are cancellable; fine-collimation Cancel calls the lock-free abort (R4); (4) `closeEvent` aborts mount
  motion lock-free before any panel disconnect (R8); (5) runner auto-unpark runs the #44 unpark enforcement (F12);
  (6) the remaining getattr probes on these paths (`move_angular`, `guide_pulse`, `guide_pulse_range_ms`,
  `abort`) use `MountCapabilities`.
- **Tests:** failing regression first for each of (1)–(5): Astronomical screen move no longer leaves tracking
  off / is refused explicitly; no emergency stop before a Terrestrial move; second concurrent motion refused;
  Cancel during reacquisition GOTO aborts and the settle ends early; quit during GOTO aborts before the
  disconnect lock; unpark → enforcement trail entry. Simulator + FakeClock, component tier.
- **Frozen:** `OnStepMountPulseAdapter`/`OnStepMountParkAdapter` public API, `MountCapabilities` fields,
  `TrackingEnforcer` existing methods, diagnostics keys, #43/#44/#46/#49 manifest assertions.
- **Dependencies:** S6.6b (done). Blocks S6.8b/S6.8c(R5)/S6.8d. External: OnStepAdapter bare
  `disable_tracking()` (R2) — recorded, not implemented.
- **Non-goals:** guide-pulse Mount Align (S6.8b), focuser Stop and a global Stop button (S6.8c), deleting
  the timed path (S6.8d), GuideTool (R10).
- **Proof:** manifest(s) with revert-proof evidence; `prove.py` for 46/49/39 manifests still PASS.

### S6.8b — Astronomical Mount Align with guide pulses (after S6.8a)

**Contract (2026-10-14).** User decision 2026-10-11: in Astronomical mode Mount Align moves with
OnStepAdapter 0.5.0 `guide_pulse` while tracking; tracking is never turned off by Mount Align.
- **Allowed:** `ui/mount_test_move_runner.py`, `ui/mount_test_move_panel.py` (binding only),
  `application/mount_align_*.py`, `application/mount_motion.py` (primitive choice only),
  `application/guide_pulse_reacquisition.py` / `equivalent_duration.py` (reuse, no behaviour change for
  reacquisition), `astrotool_core/mount/axis_calibration.py` / `calibration_matrix` owners (unit tag only),
  `testing/` simulators, their tests, new manifest(s). Not focuser, not park panel, not main_window beyond wiring.
- **Behaviour changed:** Astronomical + tracking ON + `supports_guide_pulses_while_tracking`: calibration,
  nudge, screen move and calibration returns use guide pulses through the S6.8a executor (claim, Stop,
  settle). Sizes are guide-pulse milliseconds: a calibration is a **guide-pulse calibration** (matrix tagged
  with its primitive/unit); a matrix from one primitive is never used to size moves of the other (refuse +
  "recalibrate in this mode"). A move larger than the 5000 ms per-pulse cap is split into several pulses
  (Stop checked between pulses; a pulse in flight runs to its end ≤ 5 s). The uncalibrated first move/nudge
  needs a guide-rate seed (not exposed by OnStepAdapter): use an explicit, named, configurable seed
  (single owner, templates/config.toml updated) labelled as an assumption in status/diagnostics and replaced
  by the measured value; never shown as calibrated. 0.4.1-shaped mount (no guide capability) in
  Astronomical + tracking keeps S6.8a's explicit refusal. Terrestrial unchanged (angular, tracking OFF).
  Mode change mid-run stops the run (existing).
- **Tests:** failing regression first (S6.8a refusal → guide-pulse move) against the real
  `OnStepMountPulseAdapter` + 0.5.0 simulator: astro calibration completes with tracking ON throughout and
  zero emergency stops/TRACK_OFF; nudge + screen move in astro sized in guide-ms; >5000 ms split; Stop between
  pulses; unit-mismatch refusal both directions; seed labelled; 0.4.1 refusal kept. FakeClock.
- **Frozen:** S6.8a executor API semantics, `MountCapabilities` fields, `OnStepMountPulseAdapter` API,
  reacquisition behaviour (manifests 39-*), diagnostics keys (additive only), #43/#44/#46 assertions.
- **Dependencies:** S6.8a (done). Blocks S6.8d.
- **Non-goals:** global Stop UI (S6.8c-R5), deleting the timed path (S6.8d), GuideTool, real guide-rate readout.
- **Proof:** manifest with revert-proof evidence; field-only: real guide rate vs seed, settle after guide pulses,
  supervised first astro calibration on the rig.

### S6.8c — focuser Stop (R7, done) + global mount Stop (R5)

**R5 contract (2026-10-14).**
- **Allowed:** `ui/main_window.py`, `ui/mount_park_panel.py`, a new small UI widget module if needed,
  `application/mount_motion.py` (**additive only**: e.g. an in-flight-changed notification / read-only
  `in_flight_owner`; no change to claim/abort semantics), their tests, new manifest. Not the runner, not
  Mount Align panel/application modules, not recenter (S6.8b owns those concurrently).
- **Behaviour changed:** one always-visible mount **Stop** (main window level), enabled whenever the
  executor has any mount motion in flight — park slew included — and calling the executor's lock-free
  `abort()` (no owner → stops whatever moves). Park/unpark show "stopped by user" when interrupted. Mount
  Align's own Stop keeps working. Stop never waits on `operation_lock` (GUI thread stays responsive).
- **Tests:** failing regression first: Stop during a simulated park slew reaches the mount at once
  (no in-app Stop before); button enabled exactly while a motion is in flight (park, Mount Align, reacquisition);
  Stop while idle is harmless. FakeClock / simulator.
- **Frozen:** executor claim/abort semantics, park adapter API, existing manifests.
- **Dependencies:** S6.8a (done); parallel to S6.8b on disjoint files.
- **Non-goals:** focuser (done), changing OnStepAdapter `park()` (if park cannot be interrupted, record it).
- **Proof:** manifest; field-only: real park slew stops on ABORT.

### S6.8d — dead timed path, nudge cap, stale texts (after S6.8b)

R9/F11/R11. Contract written before start. Also (S6.8a review #4): the runner's tracking gate is keyed on the step tuple shape (`len(step) > 3`), not on the primitive actually used — legacy timed path only; remove with the timed path. Stale docs (F13): panel docstring "Stop / duck-typed", `park_adapter.stop_tracking` docstring, `mount_park_panel.py:186`; `mount_commands.rearm_after_stop` getattr `clear_abort` probe.

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
| 2026-10-21 | S6.8c-R5 | ff6754e, f00007f, 9eb03dd | Global mount Stop (park slew included) reaches the mount through the shared connection; idle Stop never sends an emergency stop; interrupted park reports the user stop and any adapter error. Field incident 07d37da6 regression `8ef922a`; CI isolation fix `c3223e3` |
| 2026-10-14 | S6.8a | 85f6bca, 497459a | One mount-motion executor; no Mount Align move stops tracking on its own; Terrestrial repair via the #44 owner; Cancel/quit stop the mount; 3384 fast-tier + 1282 collimation tests green |
| 2026-10-14 | S6.8c-R7 | e028cc5, 436b312 | Focuser Stop lock-free (no longer queues behind the move or a mount GOTO), jog on a worker, blockers() non-blocking; autofocus Stop ends CANCELLED and stays put; 12 revert-proven + mutation-proven gating tests |
| 2026-10-13 | S6.6b | 1a89844 | Nudge returns immediately, #43 check in background and read-only (abandoned check never sends tracking commands); one target/registration owner both ways; capabilities instead of getattr probes; exposure snapshot owned by MainWindow; 6 new manifests (all revert-proven where a pre-fix repro exists) |
| 2026-10-12 | S6.6a | 6b6d899 | Mount Align orchestration → 10 Qt-free application modules; panel 194 kB→113 kB; reviewer's differential harness byte-identical across 13 scenarios |
| 2026-10-11 | S6.8 analysis | 1534d6d | 25 movement paths inventoried, 13 findings (docs/quality/movement-paths.md) |
| 2026-10-11 | S6.3 | b22b871 | OperationLifecycle for runners; Stop interrupts every wait (S3b xfail flipped); stuck-Busy rules unified; panel polls guarded; filter-wheel connection-loss/Alert/probe fixes; one OnStep error set |
| 2026-10-11 | S7 | 5f7c689 | Manifests for all 14 open HIGH issues with AC→test tables; 3 new unit tests; S2 gaps closed (FocuserPanel 3fc09ae, #43 guard isolation); no defects found |
| 2026-10-11 | S6.7 | 9fc3fa2 | ROLLOUT BLOCKER cleared: one owner of the fresh-frame-after-motion rule (reacquisition, autofocus, Mount Align); cancellable + capped waits; ToupTek exposure start stamped at trigger; TestMountTestMovePanel 517 s → 17 s |
| 2026-10-11 | S6.2 | 80220a6 | One device connection lifecycle (focuser, park, filter wheel, camera, GuideTool, closeEvent); internal errors still produce incident bundles; prove.py batching |
| 2026-10-07 | S6.0e | 23a9cb2 | OnStepAdapter 0.5.0 pinned; published build ≠ S6.0d pre-release (post-chunk G-flag wait) — simulator updated (494→0/3150 diffs), contract tests vs the installed controller and the real facade chain; install.md Pi upgrade with force-reinstall + build check |
| 2026-10-07 | S6.4 | be7353e | #48 + M04: global Operating Mode is the single source for Mount Align and autofocus; user decisions A (artificial star ⇒ Terrestrial) and B (NCC both modes); TrackingEnforcer hardened (lock, fail-closed reads/stops, mid-check mode switch, P-a re-enforce) |
| 2026-10-06 | S6.5 | 5f38a4c | Camera + mount capability tables; unsupported features decided once, no per-frame SDK calls; bounded range retry; TEC-off never suppressed (65,000-case exhaustive check); one owner for the cooling default |
| 2026-10-05 | S6.1 | 37db9f0, 61d1e27 | One owner for EFW name, INDI host/port and config/diagnostics paths; 2 live contradictions fixed (fake 'EFW 1', 'Filter Wheel' fallbacks); filter-wheel host → 127.0.0.1; contract scan over packages/apps/scripts; conftest isolates every reader. 61d1e27 = my hunk-split import-order slip, fixed |
| 2026-10-05 | S6.0d | feb46e5 | Astronomical reacquisition via OnStepAdapter 0.5.0 guide_pulse, capability-detected (main on 0.4.1 → clear refusal); bounded guide-pulse calibration; tracking never touched; simulator ≡ real 0.5.0 controller (3150/3150). Field: user-supervised first physical guide pulse before the pin bump |
| 2026-10-04 | S6.0b | f24a523 | Reacquisition moves the production mount on the angular path: 2-D solve (rotation/mirroring/unequal scales), per-axis cap 0.35×tracker radius, shared move planner, single ms↔arcsec owner with configured center rate, refusal reason shown, Stop latch re-armed, within-smallest-move success. Seeded sweeps + real RoiTracker; fail pre-fix / on intermediate states. Rollout blocker: fresh frame after each move (S6.7). Astronomical mode → S6.0d |
| 2026-10-04 | S6.5b | 973d0ab | NOFRAME_TIMEOUT 1 (outside vendored SDK range, inherited from INDI) → 0 (disabled; every frame wait already bounded); connect log of model.flag + derived mono/USB3-capable/USB3-over-USB2/TEC. Field: read the flag log; watch whether the open GPCMOS calibration timeout changes |
| 2026-10-03 | S3b | c55b9c6 | Runner, recenter settle and panel confirmation timeouts on the injected clock (neutral, characterization-proven); 37 fake-time tests; guard catches deadline arithmetic and timed waits in policy layers, allowlists pinned. Wall time: sizing 55.9→7.1 s, real-adapter Mount Align 95.3→11.8 s, angular 26.5→4.6 s, frame_validity 57→33 s, runner_simulated 6.7→2.4 s; failure_recovery (96 s) and TestMountTestMovePanel (8.6 min) unchanged — panel clock is S6.6 |
| 2026-10-03 | S6.5a | 4864cc8 | `is_color_sensor()` uses the SDK's real MONO flag (was USB30); all ToupTek constants from one SDK-keyed table, checked against the vendored SDK and AST-guarded; strict xfails flipped. Rig: GPCMOS known colour; G3M678M/ATR585M previous classification unknown (S6.5b flag log) |
| 2026-10-02 | S6.0c | 52fd72f | Stop is lock-free and reaches the mount mid-GOTO; GUI polls never queue (held-over readings marked not fresh); #44 gate fails closed on busy/unknown; worker decisions wait ≤0.5 s for a fresh read; next move never hit by a late stop. Tests on the OnStep simulator fail pre-fix / on the intermediate state. Field-only: real ABORT mid-GOTO; ≤~35 s until the interrupted OnStepAdapter call returns (OnStepAdapter cancel hook needed) |
| 2026-10-01 | S4 | 0fdf781 | Simulators for OnStep (0.4.1 semantics, cited), filter wheel, ToupTek (real SDK constants), frame timelines; 5 defect classes reproduced, 6 fixes revert-proven; both filter-wheel wall-clock flakes gone; regressions selectable by the changed-module gate; production ToupTek mono-flag bug pinned for S6.5a |
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
- **S4 production findings (2026-10-01), characterized not fixed.** (a) `IndiFilterWheelAdapter.status()`
  trusts its own `_connected` flag: after indiserver drops mid-move it reports cached Busy
  (`moving=True, available=True`) forever (`TestDisconnectWhileMoving`) → S6.3. (b) An out-of-range slot's
  driver Alert never reaches `FilterWheelState`; caller only learns via its 10 s timeout (`TestInvalidSlot`)
  → S6.3. (c) Seams still on the real clock / without injection: IndiFilterWheelAdapter (no client
  injection, 2 s refresh throttle on `time.monotonic`), TouptekCameraAdapter (no clock), Focuser/FilterWheel
  panel confirmation timeouts, MountTestMoveRunner (S3b). (d) Latent wall-clock flakes remain in
  `test_indi_filter_wheel_adapter.py` (`TestPropertyRefresh`, `test_a_second_move_while_the_first_is_in_progress_is_refused`);
  `FakeIndiServer` re-announces FILTER_SLOT as Ok while moving. (e) App-importing simulator workflow tests live
  in `tests/core/testing/`, so `changed_tests.py` won't select them for panel/runner/controller changes →
  move to `tests/collimation/` (S6.6).
- **S3b findings (2026-10-03).** (a) `_pulse_with_retry` never checks Stop: a rejected timed pulse is
  re-sent up to 5× (0.3 s apart) after Stop (timed-capable mounts / timed fallback only) — strict xfail
  `test_stop_during_a_rejection_retry_delay_sends_no_further_attempt` → S6.3. (b) Stop does not shorten
  waits already running (fresh-park wait, unpark confirmation, gate delay, settle, re-park) → S6.3.
  (c) `mount_test_move_panel.py` still on the real clock (completed_at, reference max age, capture-job
  timeout) → S6.6/S6.7; this, not the runner, keeps `TestMountTestMovePanel` (~8.6 min) and
  `test_calibration_failure_recovery.py` (~96 s) slow. (d) `stream_controller.py` clock seam not taken
  (allowlisted). (e) CONTRIBUTING "Deterministic time" known-gap note is now outdated for policy layers.
- **S6.0b review (2026-10-04).** Fixed in S6.0b: camera rotation on the angular reacquisition path
  (90° diverged ~12′), center_rate_x wiring, refusal reason surfaced, latched Stop re-armed by a new
  reacquisition. **ROLLOUT BLOCKER → S6.7:** reacquisition measures the latest frame after a 750 ms
  settle with no check that it was captured after the move (AGENTS.md: frames during motion are never
  evidence) — must use the fresh-frame-after-motion service before any Pi rollout.
  **USER DECISION (2026-10-04):** astronomical-mode reacquisition will use OnStepAdapter 0.5.0's
  `mount.guide_pulse(direction, ms)` (standard INDI timed-guide, tracking preserved) → task S6.0d. 0.5.0 is
  NOT published yet (local wheel only); the first physical guide-pulse acceptance is supervised by the
  user before OnStepAdapter merges/releases. Original question for the record: astronomical-mode reacquisition — OnStepAdapter 0.4.1 refuses local axis
  moves while tracking (`indi_axis_motion.py:85-97`), so with tracking on every reacquisition is refused
  (now with the reason shown). Options: (a) declare unsupported in astronomical mode with a clear UI
  message; (b) request an OnStepAdapter capability for guide-rate offsets while tracking; never stop
  tracking implicitly. Also: the policy checks no mode/tracking precondition itself (relies on the
  adapter refusing) → S6.4.
- **S6.0d review leftovers (2026-10-05) → S6.7 / field.** Guide-pulse calibration does not subtract
  tracking drift or Dec backlash (A3); the RA/Dec parallel check is permissive (`_MIN_DETERMINANT_RATIO
  1e-3`, A4); each reacquisition run recalibrates (~20 s guiding + up to 16 settled frames, A5); the #44
  evidence trail doesn't record the guide-path decision or an emergency-stop TRACK_OFF (A6). Upstream
  OnStepAdapter note: after an emergency stop, `tracking_preserved` reflects the pre-stop snapshot
  (`indi_guiding.py:220-222`) — report to the OnStepAdapter maintainer before 0.5.0 is released.
- **S6.1 follow-ups (2026-10-05).** (a) D09: `testing/fake_onstep_indi_client.py` adopts
  `config.device_defaults` (INDI_HOST/INDI_PORT) once S6.0d has landed; then delete its two pinned
  exceptions in `tests/contracts/test_config_source_contract.py`. (b) D04 cooling default (−10 °C;
  `ui/camera_panel.py`, `camera/touptek_adapter.py`, `config/camera_settings.py` twice) and D08 25 %
  calibration target (`mount/movement_sizing.py`) were tagged S6.1 in the audit but are outside S6.1's
  contract → fold into S6.5 (camera) / S6.6 (Mount Align). (c) Owner deviation from the audit:
  `config/device_defaults.py` + `config/paths.py` instead of `filter_wheel/config.py` / `indi_endpoint.py`
  (so low-level INDI code needn't import filter_wheel). (d) Field: EFW connects with the 127.0.0.1 default.
- **S6.4 findings (2026-10-05).** M02: Mount Align still has two tracking-verification paths (enforcer vs
  direct `ensure_tracking_mode` when no enforcer/mount) → S6.6. M03: `MountTestMoveRunner` turns tracking
  off before every move regardless of mode → S6.8. M05: "artificial star?" decided in three places
  (`CollimationTargetMode`, registration button, `AutofocusMode`); `_calibration_distance_m` reads the
  registration button → S6.6. `_on_calibrate_fov` doesn't itself reject star-field+Terrestrial (UI-only
  constraint). Tracking settle poll not interruptible by Stop → S6.3. P-a trigger lives in a MainWindow
  500 ms timer → move into the connection service poll in S6.2. recenter_policy doesn't ask the enforcer
  for its tracking precondition → S6.6.
- **CI note.** `37db9f0` (S6.1) failed CI on ruff I001 — my zero-context hunk split misplaced one import;
  fixed in `61d1e27`. Lesson: split shared files by building the staged blob from HEAD + exact
  replacements, never `git apply --unidiff-zero`.
- **S6.5 follow-ups (2026-10-06).** (a) D04 pinned test doubles restate −10 °C: `camera/fake_camera.py:57`,
  `camera/replay_camera.py:67`, `testing/fake_touptek.py:83` → migrate to `config.camera_settings.
  DEFAULT_TARGET_TEMPERATURE_C` in S6.6 (then delete the `_COOLING_ALLOWED` pins). (b) Callers still
  duck-typing mount capabilities (`recenter_policy.py:353/405`, `mount_test_move_panel.py:2381`, three
  `hasattr` "angular" probes + `hasattr(abort)`, `mount_test_move_runner.py:166`, `mount_park_panel.py`
  confirm_home/long_running_actions/home_confirmed, `focuser_panel.py` blockers) → S6.6; note
  `test_recenter_policy.py:231/363/404` doubles set `min_angular_arcsec = 30.0` while `capabilities()`
  says 0.0 — fix together. (c) `FocuserStatus` has no held-over flag; `sim_onstep.py` restates the 30″/10°
  bounds; camera_panel ±40 °C target-range fallback → S6.6. (d) Field: gain/exposure ranges may change
  with conversion gain on some models (ranges now cached per connection).
- **USER DECISIONS (2026-10-06, S6.4 review).** (1) Selecting Artificial Star as target switches the
  global Operating Mode to Terrestrial (tracking OFF) with a visible note — Astronomical + Artificial star
  must not exist. (2) NCC FOV registration stays available in both modes (Moon/planets at night); only
  ASTAP star-field matching is restricted to Astronomical.
- **S6.2 findings (2026-10-07).** Mount Align's connect handler still a K01 copy → S6.6 (pinned in the
  connection-failure contract test). `mount_pulse_adapter.py:138` restates the OnStep exception tuple;
  `focuser_adapter.py` converts only ValueError (K02 adapter side, makes L02 reachable) → S6.3. Periodic
  panel status polls that raise after connect are unguarded → S6.3. `IndiFilterWheelAdapter.connect` can
  leave its client open if the post-connect probe raises (service release covers it; adapter fix → S6.3).
  ToupTek enumeration cache is process-lifetime (a camera plugged in later stays invisible; pre-existing).
  **Tooling:** `prove.py --validate-all` passes all manifests' node ids in one `pytest --collect-only`
  command line → WinError 206 (command line too long) on Windows as manifests grow; batch it (S8 at the
  latest). **Process:** agents share the session scratchpad — an agent removed another agent's worktree;
  worktrees must be named per task (e.g. `wt-<task>-<role>`) and only the owner removes them.
- **USER DECISION (2026-10-10, S6.7): nudges.** A manual RA/Dec nudge returns as soon as the mount move
  completes (AGENTS.md manual-move rule); #43's no-motion check runs afterwards in the background on fresh
  frames and only adds a warning if nothing moved → S6.6 (owner of mount_test_move_panel's nudge path).
- **Coordinator decision (2026-10-10, S6.2):** the S6.4 P-a 500 ms re-enforce timer stays in MainWindow —
  it is mode/tracking policy that must tick whether or not a connection is open; the connection service is
  not its natural owner (contract deviation accepted).
- **S6.7 follow-ups → S6.6.** No GUI-thread exposure snapshot is wired into reacquisition (main_window) or
  autofocus (focuser_panel), so a first frame > ~5 s can still end in an explicit fresh_frame_timeout
  (never a stale measurement). Audit leftovers T04 (1000 vs 750 ms settle), D07, L05.
- **USER DECISION (2026-10-11, S6.8 finding F1/M03): Mount Align in Astronomical mode moves with
  OnStepAdapter 0.5.0 guide pulses while tracking** (like astronomical reacquisition); tracking is never
  turned off by Mount Align. Terrestrial keeps angular moves (tracking OFF per #44). Consequences for the
  S6.8 fix: one shared mount-motion executor choosing angular vs guide-pulse from the global mode;
  calibration/screen-move sizing in guide-pulse ms (20..5000 ms per pulse, chunked; guide rate not exposed
  → calibrate in guide-ms like S6.0d); 25%-of-frame steps may need several pulses and are slower;
  runner's unconditional `stop_tracking()` before moves removed. Other S6.8 HIGH/MEDIUM items (F3 park
  without Stop, F5 fine-collimation Cancel, F6 focuser Stop behind move lock, F7 one in-flight motion
  app-wide, F8 quit during move) are folded into the S6.8 fix contract.
- **External request (S6.8c-R5):** OnStepAdapter `park()` blocks up to 120 s with no cancel hook; an ABORT stops the slew but the call (and the app's busy state, and quit) waits it out. Ask the user at S8.
- **External request (S6.8 R2):** OnStepAdapter has no bare tracking-off; `stop()` = `ABORT`+`TRACK_OFF`.
  A `disable_tracking()` request is to be filed on OnStepAdapter (ask the user at S8); never patched locally.
- **Pre-existing flake (S8):** `test_calibration_failure_recovery.py::TestEveryFailureReturnsToIdle::
  test_failure_then_a_second_run_without_restarting[unstable_image]` (proof 49) fails ~1 in 6–8 local runs
  on HEAD and on S6.6a alike (panel stuck capturing at ~4 s) — real-time residue in that test; investigate
  in S8 before the rollout.
- **S5 live contradictions (9)** are listed in `docs/quality/duplication-audit.md`; each is owned by an
  S6 step and gets a failing regression before its fix.
