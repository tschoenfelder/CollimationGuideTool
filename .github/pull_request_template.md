## What and why

<!-- The issue(s) this addresses (Fixes #NN) and what changes. -->

## Proof

**What test would fail if this bug returned?**

<!-- Name the exact test node id(s). For a bug fix or a HIGH/CRITICAL issue, the answer
lives in a proof manifest: proofs/<issue>.toml (schema: proofs/README.md). Not needed
for documentation-only changes. -->

- Proof manifest: `proofs/____.toml`
- One-command proof: `python scripts/prove.py ____`
- Did the regression test fail before the fix (revert-proof in a separate worktree)?
  Failing assertion line, or why no pre-fix reproduction exists:
- Lowest tier covering the changed code (unit / component / contract):
- Field-only assumptions still open:

## Checklist

- [ ] A defect fix adds or strengthens a test at the lowest practical tier
      (CONTRIBUTING.md, "Test pyramid and the risk-based minimum gate").
- [ ] `python scripts/prove.py --validate-all` passes.
- [ ] `ruff check .`, `mypy .`, `lint-imports` and `scripts/check.ps1` (or `check.sh`) are green.
