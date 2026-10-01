# Proof manifests

One small TOML file per fixed defect or HIGH/CRITICAL issue (issue #54). It answers
the review question **"what test would fail if this bug returned?"** in a form CI
can check, and it gives one command that runs exactly that proof:

```text
python scripts/prove.py 49                # every manifest whose `issues` lists #49
python scripts/prove.py 47                # -> 47-efw-device-name AND diag-73c7d59d-filter-stuck-busy
python scripts/prove.py diag-7b21bdf1     # a unique file-stem prefix
python scripts/prove.py 7b21bdf1          # a unique dataset_uuids prefix (manifests without an issue)
python scripts/prove.py 49 --dry-run      # print the pytest command(s) only
python scripts/prove.py --validate-all    # what CI's fast job runs
python scripts/prove.py --list
```

**Selection rule:** a bare integer (`49` or `#49`) always means every manifest whose
`issues` lists that number, never a file stem. Anything else is the exact file stem,
else a unique stem prefix, else a unique `dataset_uuids` prefix. An ambiguous prefix
is an error that lists the candidates.

Every selected manifest's focused command runs, even after one fails. A PASS/FAIL
line per manifest is printed at the end, and the exit code is non-zero if any failed.
The focused command runs with this checkout's `packages/` and `apps/` first on
`PYTHONPATH`, so running `prove.py` inside a separate git worktree (a revert
experiment) tests that worktree's code, not the editable install.

Not required for documentation-only changes.

## File name

`proofs/<id>.toml`. Use the issue number (`49.toml`); `<issue>-<slug>.toml` when one
issue has several independent fixes (`47-efw-device-name.toml`); and
`diag-<uuid8>-<slug>.toml` for a field fix that has no GitHub issue.

## Schema

All top-level keys are required except `gaps`; unknown keys are rejected.

| Key | Type | Meaning |
|-----|------|---------|
| `issues` | list of int | GitHub issue numbers. May be empty only for a field fix without an issue, and then `dataset_uuids` must name the diagnostic. |
| `summary` | string | The defect and the fix, in two or three sentences. |
| `modules` | list of path | Production files the fix changed (`packages/...` or `apps/...`; must exist). |
| `regression_tests` | array of tables | The tests that would fail if the bug returned (below). At least one. |
| `dataset_uuids` | list of string | Diagnostic bundle UUIDs (full, or the first 8+ hex digits) the evidence came from; `[]` when none. A captured dataset under `datasets/regressions/` is referenced through its test in `regression_tests`. |
| `focused_command` | string | `pytest <flags> <test paths>` — must select every listed regression test (by node id, class or file); its paths must exist. Keep it fast: the lowest tier that proves the defect. |
| `broader_tier` | string | The tier to escalate to after the focused proof: `unit`, `component`, `contract`, `integration`, `acceptance` or `release`. |
| `field_only_assumptions` | list of string | What only a real-hardware run can still confirm; `[]` when nothing. |
| `fix_commits` | list of string | The commit(s) with the production fix (7-40 hex digits; must exist in git). |
| `gaps` | list of string | Optional. Known missing lower-tier tests or weak spots of this proof — record them honestly instead of hiding them. |

Each `[[regression_tests]]` table:

| Key | Type | Meaning |
|-----|------|---------|
| `node` | string | pytest node id under `tests/`: file, `::Class` (nested classes allowed), `::test` (`def` or `async def`, inside the named class); a parametrized test without its `[...]` id. |
| `failed_before_fix` | bool | Did this test fail against the code without the fix? |
| `evidence` | string | Required when `failed_before_fix = true`: how that was shown, and it must contain the failing line as `path.py:NNN` (e.g. from a revert-proof in a separate worktree). |
| `reason_no_prefix_repro` | string | Required when `failed_before_fix = false`: why no pre-fix failure exists or can be shown. |

### Allowed `focused_command` flags

Only flags that cannot drop a listed test are allowed:
`-q`/`-v`/`-x`/`-s`/`-l` (and combinations like `-qq`), `--quiet`, `--verbose`,
`--exitfirst`, `--showlocals`, `--tb <auto|long|short|line|native|no>`,
`-r <chars>` (`-ra`), `--durations N`, `--maxfail N`, and `-p no:<plugin>` (disabling a
plugin only). A value can be inline (`--tb=short`, `-ra`) or the next argument. Every
other option is rejected — in particular `-k`, `-m`, `--deselect`, `--ignore`,
`--co`/`--collect-only`, `--lf`, `--ff`, `--sw`, `--runxfail`, `-c`.

## The listed tests must run in CI

CI does **not** execute every focused command (that would re-run tests at extra
cost). The regression tests run in their normal tiers in the fast and slow jobs, so
validation guarantees they really run there: a listed test is rejected when its tier
(from `tier_for()` in `tests/conftest.py`, imported, not copied) is `hardware`, when it
lives in `tests/local_data/` (skipped on CI), or when it — its class or the module's
`pytestmark` — carries a static `skip`, `skipif`, `xfail` or `hardware` marker.

## What CI checks

The fast job checks out the full history (`fetch-depth: 0`) and runs
`python scripts/prove.py --validate-all`: schema, flag whitelist, files, node ids
(statically and via `pytest --collect-only`), markers and tiers, and every fix commit
in git. A missing fix commit is an error; only in a shallow clone is it a loud
`SHALLOW CLONE ... NOT VERIFIED` warning. `tests/contracts/test_proof_manifests.py`
runs the same checks without collection.

## Revert-proof

To fill `evidence`, never revert in the shared working tree. Create the worktree from
the CURRENT `HEAD` — it already contains `scripts/prove.py` and `proofs/` — and undo
only the fix, keeping today's tests:

```text
git worktree add --detach ../cgt-proof-wt HEAD
cd ../cgt-proof-wt
git revert -n <fix commit>          # or undo just the fix hunk by hand on a conflict
git checkout HEAD -- tests          # keep today's tests
python scripts/prove.py <id>        # must FAIL now -- copy the failing path.py:NNN line
cd - && git worktree remove --force ../cgt-proof-wt
```

Do not check out the old pre-fix commit instead: it predates `prove.py`, the manifests
and often today's tests. If you have to, print the arguments with
`python scripts/prove.py <id> --dry-run` in the main tree and run
`python -m pytest <those arguments>` in the worktree with `PYTHONPATH=packages;apps`
(`:` on Linux) set, then check the imported module's `__file__` points into the worktree.
