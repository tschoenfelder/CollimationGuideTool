# Test tier timings

Measured for issue #50 (test tiers and fast gate) on 2026-09-30, on the
Windows 11 development machine (Python 3.13.13, the repo's `.venv`), one
run each, other work idle. Wall-clock seconds for the `pytest` step only;
ruff + mypy (warm cache) + import-linter together add about 3 s to every
`scripts/check.*` mode. `tests/local_data/` was present on this machine
(it is local-only; on CI those tests skip).

## Before (global coverage on every run)

| Command | Tests | Wall-clock |
|---------|-------|-----------:|
| default gate: `pytest tests/core tests/collimation tests/guide` (with forced `--cov`) | 1498 passed, 3 skipped | 869 s (14.5 min) |
| release gate: `+ tests/contracts tests/integration tests/regressions tests/acceptance` (with `--cov`) | 1656 passed, 13 skipped | 878 s (14.6 min) |

Before this change there was no smaller command: every `pytest` invocation,
even of a single file, also measured coverage of the whole repository and
applied the 80 % threshold to the result, so any targeted run failed (even
`pytest --collect-only` printed "FAIL Required test coverage of 80% not
reached. Total coverage: 32.13%").

## After

Tier runs. `unit`, `contract` and `component` were re-measured on
2026-10-01, after `changed_tests.py`'s own tests (which call git) moved to
`component`. The rest are from 2026-09-30.

| Tier / command | Tests | Wall-clock |
|----------------|-------|-----------:|
| `pytest -m unit` | 603 passed, 2 skipped | 33 s |
| `pytest -m contract` | 83 passed | 3 s |
| `pytest -m component` (measured on its own) | 971 passed, 1 skipped | 801 s (13.4 min) |
| **fast gate** `pytest -m "unit or component or contract"` (2026-09-30) | 1638 passed, 3 skipped | 866 s (14.4 min) |
| `pytest -m integration` | 167 passed, 4 skipped, 11 xfailed | 109 s |
| `pytest -m acceptance` | 23 passed | 4 s |
| **slow gate** `pytest -m "integration or acceptance"` | 190 passed, 4 skipped, 11 xfailed | 119 s |
| `pytest -m hardware` | 6, skipped unless `--run-hardware` | — |
| **release gate** `scripts/check.* --release` test step, with `--cov` (95.36 %, fail-under 80 met) | 1715 passed, 13 skipped | 863 s (14.4 min) |

Default `scripts/check.*` mode for a change to **one production file only**.
The table shows exactly what `scripts/changed_tests.py` selects; the
selection itself takes ~0.2 s. Wall-clock is for the pytest process, lint
excluded.

| Change | `changed_tests.py` selects | Tests | Wall-clock |
|--------|----------------------------|-------|-----------:|
| pure domain: `apps/collimation_tool/domain/collimation_state.py` | `tests/collimation/domain/test_collimation_state.py` | 25 passed | 1.5 s |
| UI panel: `apps/collimation_tool/ui/filter_wheel_panel.py` | `tests/collimation/ui/test_filter_wheel_panel.py` | 21 passed | 1.6 s |
| UI panel: `apps/collimation_tool/ui/focuser_panel.py` | `tests/collimation/ui/test_focuser_panel.py` | 23 passed | 1.7 s |
| hardware adapter: `packages/astrotool_core/onstep/connection.py` | `tests/contracts` + `tests/core/onstep` | 131 passed, 6 hardware skipped | 3.1 s |

The domain run touches no UI or guide suite. The adapter run needs no Pi,
indiserver or device (fakes only).

## What the numbers say

- **Coverage costs almost nothing here.** The default gate with coverage
  took 869 s and the fast tiers without it 866 s; the time is in the tests
  themselves.
- **The fast gate as a whole is not yet fast.** The `component` tier takes
  801 s, about 95 % of it. In `--durations` its 15 slowest tests (14 of
  them `TestMountTestMovePanel` tests in
  `tests/collimation/ui/test_collimation_main_window.py`, plus one in
  `test_calibration_failure_recovery.py`) take 459 s together: 23–74 s
  each, spent in real settle/wait time. To get a whole-tier run down to
  "seconds for a component change", those waits need to move to fake time
  (#53) and simulators (#51); new tooling won't do it.
- **Tiers already make a single change fast to prove.** Its own tests run
  in about 1.5–3 s instead of 14.5 min, and a single-file run no longer
  fails on global coverage. The full suite is an escalation and release
  gate.

## Design targets per tier

| Tier | Target feedback |
|------|-----------------|
| targeted (changed modules) | seconds |
| `unit` | < 1 min for the whole tier (33 s today) |
| `contract` | seconds (3 s today) |
| `component` | minutes for the whole tier; seconds per module |
| `integration` + `acceptance` | a few minutes (2 min today) |
| release gate with coverage | CI-scale; not the first proof of a change |

## Recent field defects and the lowest tier that should catch them

| Defect (fix commit) | Lowest tier | Why that tier |
|---------------------|-------------|---------------|
| Crash when OnStep `connect()` times out in the Focuser / MountPark / MountTestMove panels, then the caught failure was shown but lost (`7bb7b68`, `ca5bb92`) | `component` | One panel with a fake adapter whose `connect()` raises; assert the panel survives, shows and logs the failure. No hardware, no event-loop timing. |
| "Set filter" stuck forever when the wheel reports Busy forever (`3fc09ae`) | `component` | The filter-wheel panel against a fake wheel that never leaves Busy, with a fake clock advancing past the confirmation timeout. |
| Concurrent operations on the shared OnStep connection (`9cea2e9`) | `contract` (+ `component` for the adapters) | Every OnStep adapter must serialize on the one shared connection; a simulator that detects overlapping calls makes this a deterministic conformance check. |
| Continuous SDK-call spam on cameras lacking a queried capability (`531a952`) | `component` | The ToupTek adapter against a fake SDK without the capability; assert the failing call is made once, not per poll. |

These fixes shipped with tests in the listed tiers' directories
(`tests/collimation/ui/`, `tests/core/onstep/`, `tests/core/camera/`). The
deterministic fake-clock and simulator versions of these defect classes are
delivered by #53 (injected clock/scheduler) and #51 (device simulators).

Production deployment and real hardware are final validation, never the
primary proof of a fix.
