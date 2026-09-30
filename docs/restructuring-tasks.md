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
| S1 | #50 | Test tier markers, fast/changed gate, coverage split, CI jobs, timing report | | | todo | | |
| S2 | #54 | Proof manifests, `scripts/prove.py`, CI validation, 5 backfills (3 revert-proven) | | | todo | | |
| S3 | #53, #13 | `astrotool_core.timing` (Clock/FakeClock/Deadline/Scheduler); migrate timing modules; sleep guard | | | todo | | |
| S4 | #51 | Scenario simulators (OnStep mount, focuser, filter wheel, ToupTek camera) on FakeClock; 5 defect reproductions | | | todo | | |
| S5 | #55, #52 | Duplication inventory + size/dependency baseline (`docs/quality/duplication-audit.md`) | | | todo | | |
| S6.1 | #55 | Single-source device defaults + config-source contract test | | | todo | | |
| S6.2 | #52 | `DeviceConnectionService` (Focuser → MountPark → MountTestMove) | | | todo | | |
| S6.3 | #52 | `OperationLifecycle` + bounded Busy timeout (FilterWheel, Focuser) | | | todo | | |
| S6.4 | #48 | Remove Mount Align local Star/Terrestrial toggle; derive from global OperatingMode | | | todo | | |
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

## Done log

| Date | Task | Commit | What was proven |
|------|------|--------|-----------------|
| 2026-09-30 | S0 | (this commit) | docs only — guidelines now state the tiered gate, deterministic-time, duplicated-knowledge, proof-manifest, layering and agent-ownership rules |

## Outstanding / field-only assumptions

*(Filled per increment — anything that only real hardware can confirm.)*

## Discovered dependencies

*(Out-of-scope findings recorded instead of acted on.)*
